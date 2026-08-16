"""The Ledger facade — the only public surface (design §13).

Two constructors: `Ledger.open(path)` owns its connection (tests, CLI,
corpus runs); `Ledger(connection)` borrows one the caller owns, so an
embedding application can wrap a ledger write and its own write in a
single SQLite transaction. The library touches only its own tables either
way.
"""

import contextlib
import dataclasses
import datetime
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Dev-only interop; imported lazily at call time so the runtime path
    # never depends on beancount.
    from ledger.interop.import_ import ImportReport

from ledger.domain.accounts import Account, AccountType, type_for_path
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.balancing import ResolvedLeg, balance_transaction, resolve_leg
from ledger.domain.booking import (
    AvailableLot,
    BookingMethod,
    LotMatch,
    book_reduction,
)
from ledger.domain.directives import (
    PRICE_ORIGINS,
    AssertionStatus,
    BalanceAssertion,
    Document,
    Event,
    Note,
    Pad,
    PricePoint,
    evaluate_assertion,
)
from ledger.domain.errors import (
    AccountNotOpenError,
    AlreadyReversedError,
    CommodityMismatchError,
    CommodityNotAllowedError,
    DocumentError,
    DuplicateSourceError,
    EventError,
    InvalidPriceError,
    InvalidTransactionError,
    NoteError,
    OptionError,
    PadError,
    ReversalError,
    UnknownAccountError,
    UnknownCommodityError,
    UnknownTransactionError,
)
from ledger.domain.inventory import Inventory, Lot
from ledger.domain.transaction import (
    PAD_SOURCE,
    RESERVED_SOURCES,
    REVERSAL_SOURCE,
    Posting,
    Transaction,
    TransactionSpec,
    validate_links,
    validate_tags,
)
from ledger.interop.export import export_string as _export_beancount_string
from ledger.query.balances import balance as _query_balance
from ledger.query.balances import balance_value_before
from ledger.query.balances import inventory as _query_inventory
from ledger.query.journal import JournalEntry
from ledger.query.journal import journal as _query_journal
from ledger.query.statements import BalanceSheet, IncomeStatement
from ledger.query.statements import balance_sheet as _query_balance_sheet
from ledger.query.statements import income_statement as _query_income_statement
from ledger.query.valuation import market_value as _query_market_value
from ledger.query.valuation import unrealized_gain as _query_unrealized_gain
from ledger.storage.db import connect, migrate, unit_of_work
from ledger.storage.repositories import Repository, SQLiteRepository
from ledger.validation.validator import ValidationReport
from ledger.validation.validator import validate as _validate

KNOWN_OPTIONS = frozenset(
    {
        "operating_currency",
        "inferred_tolerance_multiplier",
        "default_booking_method",
        "gains_account_root",
        "opening_balances_account",
    }
)


class Ledger:
    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        repository: Repository | None = None,
    ) -> None:
        """Wrap a caller-owned SQLite connection (the embedded case), or —
        mainly for tests — a Repository directly."""
        if (connection is None) == (repository is None):
            raise ValueError("provide exactly one of connection or repository")
        if connection is not None:
            migrate(connection)
            repository = SQLiteRepository(connection)
        assert repository is not None
        self._repo = repository
        self._conn = connection
        self._owns_connection = False

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> Ledger:
        """Open (creating and migrating as needed) a ledger database file,
        owning its connection."""
        ledger = cls(connect(path))
        ledger._owns_connection = True
        return ledger

    def close(self) -> None:
        if self._owns_connection and self._conn is not None:
            self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- setup -------------------------------------------------------------

    def create_commodity(
        self,
        symbol: str,
        kind: CommodityKind | str,
        display_precision: int = 2,
        *,
        name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Commodity:
        if isinstance(kind, str):
            kind = CommodityKind(kind.lower())
        commodity = Commodity(
            symbol=symbol, kind=kind, display_precision=display_precision
        )
        self._repo.add_commodity(commodity, name=name, metadata=metadata)
        return commodity

    def create_account(
        self,
        path: str,
        type: AccountType | str,
        opened_on: datetime.date,
        *,
        booking_method: BookingMethod | str = BookingMethod.STRICT,
        allowed_commodities: Iterable[str] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Account:
        if isinstance(type, str):
            type = AccountType(type.upper())
        if isinstance(booking_method, str):
            booking_method = BookingMethod(booking_method.upper())
        account = Account(
            path=path,
            type=type,
            opened_on=opened_on,
            booking_method=booking_method,
            allowed_commodities=frozenset(allowed_commodities)
            if allowed_commodities is not None
            else None,
            metadata=dict(metadata) if metadata else {},
        )
        self._repo.add_account(account)
        return account

    def close_account(self, path: str, closed_on: datetime.date) -> None:
        self._repo.close_account(path, closed_on)

    def get_account(self, path: str) -> Account | None:
        return self._repo.get_account(path)

    def list_accounts(self) -> list[Account]:
        return self._repo.list_accounts()

    def get_commodity(self, symbol: str) -> Commodity | None:
        return self._repo.get_commodity(symbol)

    def list_commodities(self) -> list[Commodity]:
        return self._repo.list_commodities()

    def set_option(self, key: str, value: str) -> None:
        if key not in KNOWN_OPTIONS:
            raise OptionError(f"unknown option {key!r}; known: {sorted(KNOWN_OPTIONS)}")
        if key == "inferred_tolerance_multiplier":
            try:
                if Decimal(value) < 0:
                    raise OptionError("inferred_tolerance_multiplier must be >= 0")
            except InvalidOperation as exc:
                raise OptionError(
                    f"inferred_tolerance_multiplier must be a decimal, got {value!r}"
                ) from exc
        self._repo.set_option(key, value)

    def get_option(self, key: str) -> str | None:
        return self._repo.get_option(key)

    def _write_scope(self) -> contextlib.AbstractContextManager[None]:
        """One atomic unit for multi-step writes (assertion + padding). A
        no-op for the in-memory backend; joins the caller's transaction on
        a borrowed connection."""
        if self._conn is not None:
            return unit_of_work(self._conn)
        return contextlib.nullcontext()

    def _tolerance_multiplier(self) -> Decimal:
        return Decimal(self._repo.get_option("inferred_tolerance_multiplier") or "1")

    # --- writing -----------------------------------------------------------

    def record(self, spec: TransactionSpec) -> Transaction:
        """Validate, interpolate, balance, and store one transaction
        atomically. Either the whole balanced transaction lands or none of
        it does.

        The sequence never changes across milestones — later work inserts
        steps, it does not reorder them (plan §1.6):

            validate accounts exist and are open on the date
            resolve booking (M7: reductions matched against stored lots)
            compute weights / infer tolerances / interpolate  [balancing]
            assert balanced per commodity
            write atomically (postings, lots, reductions, implied prices)

        The generated realized-gain posting of a sale rides the existing
        interpolation: leave the gains leg open and the residual between
        cost-basis weight and proceeds fills it (design §7).
        """
        resolved = self._resolve(spec)
        with self._write_scope():
            stored = self._repo.add_transaction(resolved)
            self._observe_prices(stored)
        return stored

    def _require_commodity(self, symbol: str) -> Commodity:
        registered = self._repo.get_commodity(symbol)
        if registered is None:
            raise UnknownCommodityError(symbol)
        return registered

    def _check_registered(self, amount: Amount) -> None:
        """The amount's commodity must be registered and agree with its
        registered definition."""
        symbol = amount.commodity.symbol
        if self._require_commodity(symbol) != amount.commodity:
            raise CommodityMismatchError(
                f"commodity {symbol!r} in posting disagrees with its"
                f" registered definition"
            )

    def _maybe_create_gains_account(
        self, path: str, opened_on: datetime.date
    ) -> Account | None:
        """Gains accounts must exist before the first sale, so the ledger
        creates them on demand under the configured root (design §7). The
        account opens on the date of the transaction that needed it."""
        root = self._repo.get_option("gains_account_root")
        if root is None or (path != root and not path.startswith(root + ":")):
            return None
        account = Account(path=path, type=type_for_path(path), opened_on=opened_on)
        self._repo.add_account(account)
        return account

    def _available_lots(
        self,
        account: str,
        symbol: str,
        on_date: datetime.date,
        adjustments: Mapping[int, int] | None,
    ) -> list[AvailableLot]:
        """The lots a reduction on `account` may draw from: recorded no
        later than the transaction's date, with remaining quantity over
        *all* stored reductions — consumption is materialized at entry
        time and permanent (design §16), the same stance as pads, so a
        later-dated sale that already spent a lot keeps it spent.
        `adjustments` overlays not-yet-written deltas (a pending reversal
        in replace(), earlier reductions in the same transaction)."""
        lots = self._repo.list_lots(account, symbol)
        remaining = {
            lot.id: lot.original_quantity for lot in lots if lot.id is not None
        }
        for entry in self._repo.list_lot_reductions(lot_ids=set(remaining)):
            remaining[entry.lot_id] -= entry.quantity
        available = []
        for lot in lots:
            assert lot.id is not None
            left = remaining[lot.id] + (
                adjustments.get(lot.id, 0) if adjustments else 0
            )
            if left > 0 and lot.recorded_on is not None and lot.recorded_on <= on_date:
                available.append(AvailableLot(lot=lot, remaining=left))
        return available

    def _resolve(
        self,
        spec: TransactionSpec,
        lot_adjustments: Mapping[int, int] | None = None,
    ) -> Transaction:
        """Validate, book, and balance a spec into an unsaved committed
        transaction — record() minus the write, shared with replace()
        (which passes the pending reversal's lot restorations as
        `lot_adjustments`)."""
        if len(spec.postings) < 2:
            raise InvalidTransactionError("a transaction needs at least two postings")
        if spec.source in RESERVED_SOURCES:
            raise InvalidTransactionError(
                f"source {spec.source!r} is reserved for ledger-generated transactions"
            )

        accounts: dict[str, Account] = {}
        for posting_spec in spec.postings:
            account = accounts.get(posting_spec.account)
            if account is None:
                found = self._repo.get_account(posting_spec.account)
                if found is None:
                    found = self._maybe_create_gains_account(
                        posting_spec.account, spec.date
                    )
                if found is None:
                    raise UnknownAccountError(posting_spec.account)
                account = found
                accounts[posting_spec.account] = account
            if not account.is_open_on(spec.date):
                raise AccountNotOpenError(
                    f"account {account.path!r} is not open on {spec.date}"
                    f" (open {account.opened_on}"
                    f"{f', closed {account.closed_on}' if account.closed_on else ''})"
                )
            if posting_spec.units is not None:
                self._check_registered(posting_spec.units)
            if posting_spec.price is not None:
                self._check_registered(posting_spec.price)

        # Normalize costs and prices, and book reductions against stored
        # lots. `adjustments` carries the caller's overlay plus what
        # earlier postings of this same transaction consume, so a second
        # reduction sees the first one's effect.
        adjustments: dict[int, int] = dict(lot_adjustments or {})
        legs: list[ResolvedLeg] = []
        for posting_spec in spec.postings:
            cost_commodity = None
            if (
                posting_spec.cost is not None
                and posting_spec.cost.commodity is not None
            ):
                cost_commodity = self._require_commodity(posting_spec.cost.commodity)
            cost, price = resolve_leg(posting_spec, cost_commodity, spec.date)
            booking = None
            if (
                cost is not None
                and posting_spec.units is not None
                and posting_spec.units.value < 0
            ):
                account = accounts[posting_spec.account]
                booking = book_reduction(
                    self._available_lots(
                        account.path,
                        posting_spec.units.commodity.symbol,
                        spec.date,
                        adjustments,
                    ),
                    posting_spec.units,
                    cost,
                    account.booking_method,
                )
                for match in booking.matches:
                    adjustments[match.lot_id] = (
                        adjustments.get(match.lot_id, 0) - match.quantity
                    )
            legs.append(ResolvedLeg(cost=cost, price=price, booking=booking))

        postings = balance_transaction(
            spec,
            tolerance_multiplier=self._tolerance_multiplier(),
            legs=legs,
        )

        # Checked after balancing so interpolated postings are covered too.
        for posting in postings:
            allowed = accounts[posting.account].allowed_commodities
            if allowed is not None and posting.units.commodity.symbol not in allowed:
                raise CommodityNotAllowedError(
                    f"account {posting.account!r} does not allow"
                    f" {posting.units.commodity.symbol}"
                    f" (allowed: {sorted(allowed)})"
                )

        return Transaction(
            date=spec.date,
            postings=tuple(postings),
            flag=spec.flag,
            payee=spec.payee,
            narration=spec.narration,
            tags=validate_tags(spec.tags),
            links=validate_links(spec.links),
            source=spec.source,
            source_ref=spec.source_ref,
            created_at=datetime.datetime.now(datetime.UTC),
            metadata=dict(spec.metadata),
        )

    def _observe_prices(self, transaction: Transaction) -> None:
        """Record the price points a transaction implies (design §9) with
        origin='transaction': an explicit `@ price` always, an
        acquisition's cost when no price is given. Never overwrites an
        existing row for the same (date, commodity, quote) — an explicit
        directive beats an implied observation. Reductions' costs are not
        observed: a lot's historical cost says nothing about the market
        on the day it was sold."""
        for posting in transaction.postings:
            observed = posting.price
            if (
                observed is None
                and posting.cost is not None
                and posting.units.value > 0
                and not posting.lot_matches
            ):
                observed = posting.cost.per_unit
            if observed is None or observed.value <= 0:
                continue
            if observed.commodity.symbol == posting.units.commodity.symbol:
                continue
            self._repo.add_price(
                PricePoint(
                    date=transaction.date,
                    commodity=posting.units.commodity.symbol,
                    price=observed,
                    origin="transaction",
                ),
                replace=False,
            )

    # --- corrections (design §11) ------------------------------------------

    def reverse(
        self,
        transaction_id: int,
        on_date: datetime.date,
        reason: str | None = None,
    ) -> Transaction:
        """Reverse a committed transaction: a new transaction negating every
        posting, dated `on_date` — the date of discovery, never backdated —
        and linked via `reverses_id`. The original stays queryable forever;
        balances from `on_date` onward return to their prior values.

        The reversal carries source 'reversal' and the original's id as its
        source_ref, so the unique source index enforces one reversal per
        transaction and `get_transaction_by_source` finds it.
        """
        original = self._require_reversible(transaction_id, on_date)
        return self._repo.add_transaction(
            self._build_reversal(original, on_date, reason)
        )

    def replace(
        self,
        transaction_id: int,
        new: TransactionSpec,
        *,
        on_date: datetime.date | None = None,
    ) -> Transaction:
        """Correct a committed transaction: reverse it and record `new` in
        one atomic write, both linked to the original via `reverses_id`.
        The reversal is dated `on_date` (defaulting to `new.date`), which
        must not predate the original. Returns the replacement.

        The original keeps its (source, source_ref) forever, so
        re-ingesting the old ref stays deduplicated; the replacement must
        carry its own ref, or none — a Plaid pending→posted revision, where
        the posted transaction arrives under a new id, is the natural shape.
        """
        if on_date is None:
            on_date = new.date
        original = self._require_reversible(transaction_id, on_date)
        # The reversal is built first so the replacement's booking sees
        # the lot state it will leave behind (restored reductions,
        # re-consumed acquisitions) — without writing anything yet.
        reversal = self._build_reversal(original, on_date, "replaced")
        adjustments: dict[int, int] = {}
        for posting in reversal.postings:
            for match in posting.lot_matches:
                adjustments[match.lot_id] = (
                    adjustments.get(match.lot_id, 0) - match.quantity
                )
        replacement = dataclasses.replace(
            self._resolve(new, lot_adjustments=adjustments),
            reverses_id=original.id,
        )
        # Everything that could fail is checked before anything is written:
        # the in-memory backend has no rollback, so a failing second insert
        # must be impossible rather than rolled back.
        if replacement.source_ref is not None:
            existing = self._repo.get_transaction_by_source(
                replacement.source, replacement.source_ref
            )
            if existing is not None:
                raise DuplicateSourceError(
                    f"transaction with source={replacement.source!r},"
                    f" source_ref={replacement.source_ref!r} already exists"
                    f" (id {existing.id}); the replacement needs its own"
                    f" source_ref — the original keeps its ref for dedup"
                )
        with self._write_scope():
            self._repo.add_transaction(reversal)
            stored = self._repo.add_transaction(replacement)
            self._observe_prices(stored)
        return stored

    def _require_reversible(
        self, transaction_id: int, on_date: datetime.date
    ) -> Transaction:
        original = self._repo.get_transaction(transaction_id)
        if original is None:
            raise UnknownTransactionError(f"no transaction with id {transaction_id}")
        if original.source == REVERSAL_SOURCE:
            raise ReversalError(
                f"transaction {transaction_id} is itself a reversal; to undo"
                f" it, record the original transaction again"
            )
        if original.generated:
            raise ReversalError(
                f"transaction {transaction_id} is machine-generated"
                f" (source {original.source!r}); correct its originating"
                f" directive instead"
            )
        if (
            self._repo.get_transaction_by_source(REVERSAL_SOURCE, str(transaction_id))
            is not None
        ):
            raise AlreadyReversedError(
                f"transaction {transaction_id} is already reversed"
            )
        if on_date < original.date:
            raise ReversalError(
                f"cannot correct transaction {transaction_id} on {on_date},"
                f" before its own date {original.date}; corrections are dated"
                f" on the date of discovery, never backdated"
            )
        for path in sorted({posting.account for posting in original.postings}):
            account = self._repo.get_account(path)
            assert account is not None  # the original posted to it
            if not account.is_open_on(on_date):
                raise AccountNotOpenError(
                    f"cannot reverse into {path!r} on {on_date}"
                    f" (open {account.opened_on}"
                    f"{f', closed {account.closed_on}' if account.closed_on else ''})"
                )
        for seq, posting in enumerate(original.postings):
            if (
                posting.cost is not None
                and posting.units.value > 0
                and not posting.lot_matches
            ):
                lot = self._repo.get_lot_by_opening(transaction_id, seq)
                assert lot is not None and lot.id is not None  # it opened one
                consumed = sum(
                    entry.quantity
                    for entry in self._repo.list_lot_reductions(lot_ids={lot.id})
                )
                if consumed != 0:
                    raise ReversalError(
                        f"cannot reverse transaction {transaction_id}: its"
                        f" lot of {posting.units.to_decimal()}"
                        f" {posting.units.commodity.symbol}"
                        f" {{{lot.cost.to_decimal()}"
                        f" {lot.cost.commodity.symbol}, {lot.acquired_on}}}"
                        f" has since been reduced; reverse the dependent"
                        f" reductions first (design §11)"
                    )
        return original

    def _build_reversal(
        self,
        original: Transaction,
        on_date: datetime.date,
        reason: str | None,
    ) -> Transaction:
        """The exact negation of `original`, posting for posting, in the
        original's order — lots included (design §11): a posting that
        reduced lots gets negated matches, restoring them; a posting that
        opened a lot gets a full consumption of that lot, closing it. The
        caller (_require_reversible) has already refused originals whose
        lots were reduced in the meantime.

        The reversal carries the original's tags and links, so tag-sliced
        queries see the correction cancel inside the slice rather than
        the original's effect surviving it.
        """
        assert original.id is not None
        postings = []
        for seq, posting in enumerate(original.postings):
            matches: tuple[LotMatch, ...] = ()
            if posting.lot_matches:
                matches = tuple(
                    LotMatch(lot_id=match.lot_id, quantity=-match.quantity)
                    for match in posting.lot_matches
                )
            elif posting.cost is not None and posting.units.value > 0:
                lot = self._repo.get_lot_by_opening(original.id, seq)
                assert lot is not None and lot.id is not None
                matches = (LotMatch(lot_id=lot.id, quantity=posting.units.value),)
            postings.append(
                Posting(
                    account=posting.account,
                    units=-posting.units,
                    weight=-posting.weight,
                    cost=posting.cost,
                    price=posting.price,
                    lot_matches=matches,
                )
            )
        suffix = f": {reason})" if reason else ")"
        return Transaction(
            date=on_date,
            postings=tuple(postings),
            narration=f"(Reversal of transaction {original.id}{suffix}",
            tags=original.tags,
            links=original.links,
            source=REVERSAL_SOURCE,
            source_ref=str(original.id),
            reverses_id=original.id,
            created_at=datetime.datetime.now(datetime.UTC),
        )

    def get_transaction_by_source(
        self, source: str | None, source_ref: str
    ) -> Transaction | None:
        """The transaction carrying (source, source_ref), if any — how the
        product finds an already-ingested transaction by its Plaid ref, and
        how a reversal is found (source 'reversal', ref str(original id))."""
        return self._repo.get_transaction_by_source(source, source_ref)

    # --- assertions and pads (design §8) -----------------------------------

    def assert_balance(
        self,
        account: str,
        date: datetime.date,
        amount: Decimal,
        commodity: str,
        *,
        source: str | None = None,
    ) -> BalanceAssertion:
        """Assert that `account` (sub-accounts included) holds `amount` of
        `commodity` at the *start* of `date`, then store and immediately
        evaluate the assertion. The outcome is data on the returned
        assertion (`status`, `difference`) — a failure is not an exception.

        Evaluation is what springs pads: an unconsumed pad on this account
        dated before `date` generates the balancing transaction if the
        assertion would otherwise fail, and is spent either way.
        """
        found = self._repo.get_account(account)
        if found is None:
            raise UnknownAccountError(account)
        if date < found.opened_on:
            raise AccountNotOpenError(
                f"cannot assert a balance for {account!r} on {date},"
                f" before it opened on {found.opened_on}"
            )
        registered = self._repo.get_commodity(commodity)
        if registered is None:
            raise UnknownCommodityError(commodity)
        with self._write_scope():
            assertion = self._repo.add_assertion(
                BalanceAssertion(
                    date=date,
                    account=account,
                    amount=Amount.from_decimal(amount, registered),
                    source=source,
                )
            )
            return self._evaluate_assertion(assertion)

    def pad(
        self,
        account: str,
        source_account: str,
        date: datetime.date,
    ) -> Pad:
        """Arm an automatic balancing transaction: when the next assertion
        on `account` dated after `date` is evaluated, any difference is
        booked from `source_account` in a transaction dated `date` (flag
        'P', generated, source 'pad').

        The pad must be dated strictly before the assertion it serves — an
        assertion checks the start of its date, which a same-day padding
        transaction cannot reach (matching Beancount). Pads match their
        account exactly: a pad on a parent is not consumed by an assertion
        on a child (a deliberate divergence from Beancount, where that
        combination inserts padding on the parent and still fails the
        child's check).
        """
        if account == source_account:
            raise PadError(f"cannot pad {account!r} from itself")
        for path in (account, source_account):
            found = self._repo.get_account(path)
            if found is None:
                raise UnknownAccountError(path)
            if not found.is_open_on(date):
                raise AccountNotOpenError(f"account {path!r} is not open on {date}")
        return self._repo.add_pad(
            Pad(date=date, account=account, source_account=source_account)
        )

    def check_assertions(self) -> list[BalanceAssertion]:
        """Re-evaluate every stored assertion in (date, id) order, writing
        fresh status, difference, and checked_at back to each row. Pads
        are consumed by the same rules as at entry time, so an assertion
        stored before its pad reconciles here. Idempotent."""
        with self._write_scope():
            return [
                self._evaluate_assertion(assertion)
                for assertion in self._repo.list_assertions()
            ]

    def _evaluate_assertion(self, assertion: BalanceAssertion) -> BalanceAssertion:
        assert assertion.id is not None
        multiplier = self._tolerance_multiplier()
        symbol = assertion.amount.commodity.symbol

        def compare() -> tuple[AssertionStatus, Amount]:
            actual = balance_value_before(
                self._repo, assertion.account, assertion.date, symbol
            )
            return evaluate_assertion(assertion.amount, actual, multiplier)

        status, difference = compare()

        # Pads dated strictly before the assertion, on exactly this
        # account, not yet spent. All of them are spent by this
        # evaluation; only the latest generates (Beancount's rule — an
        # earlier pad superseded before any balance check is "unused").
        eligible = [
            pad
            for pad in self._repo.list_pads(account=assertion.account)
            if pad.consumed_by is None and pad.date < assertion.date
        ]
        generated: Transaction | None = None
        if eligible and status is AssertionStatus.FAIL:
            generated = self._create_padding(eligible[-1], assertion, difference)
            status, difference = compare()
        for pad in eligible:
            generated_id = (
                generated.id if generated is not None and pad is eligible[-1] else None
            )
            assert pad.id is not None
            self._repo.consume_pad(pad.id, assertion.id, generated_id)

        checked_at = datetime.datetime.now(datetime.UTC)
        self._repo.update_assertion_check(
            assertion.id, status, difference.value, checked_at
        )
        return dataclasses.replace(
            assertion, status=status, difference=difference, checked_at=checked_at
        )

    def _create_padding(
        self,
        pad: Pad,
        assertion: BalanceAssertion,
        difference: Amount,
    ) -> Transaction:
        """The balancing transaction a pad promises: asserted - actual
        into the padded account, the negation from the source account,
        dated on the pad's date."""
        assert pad.id is not None
        commodity = assertion.amount.commodity
        needed = Amount.from_scaled(-difference.value, commodity)
        transaction = Transaction(
            date=pad.date,
            postings=(
                Posting(account=pad.account, units=needed, weight=needed),
                Posting(account=pad.source_account, units=-needed, weight=-needed),
            ),
            flag="P",
            narration=(
                f"(Padding inserted for balance of"
                f" {assertion.amount.to_decimal()} {commodity.symbol}"
                f" on {assertion.date})"
            ),
            source=PAD_SOURCE,
            source_ref=str(pad.id),
            generated=True,
            created_at=datetime.datetime.now(datetime.UTC),
        )
        return self._repo.add_transaction(transaction)

    def get_assertion(self, assertion_id: int) -> BalanceAssertion | None:
        return self._repo.get_assertion(assertion_id)

    def list_assertions(self, account: str | None = None) -> list[BalanceAssertion]:
        return self._repo.list_assertions(account)

    def list_pads(self, account: str | None = None) -> list[Pad]:
        return self._repo.list_pads(account)

    # --- hub attachments (design §10, plan §8) -----------------------------

    def _require_account_active(self, account: str, date: datetime.date) -> None:
        """The account must exist and `date` must not predate its open —
        the same rule as balance assertions (dating after close is fine;
        Beancount agrees, verified against 3.2.3)."""
        found = self._repo.get_account(account)
        if found is None:
            raise UnknownAccountError(account)
        if date < found.opened_on:
            raise AccountNotOpenError(
                f"account {account!r} opened on {found.opened_on}, after {date}"
            )

    def add_note(self, account: str, date: datetime.date, comment: str) -> Note:
        """Attach a dated comment to an account."""
        self._require_account_active(account, date)
        if not comment.strip():
            raise NoteError("a note needs a non-empty comment")
        return self._repo.add_note(Note(date=date, account=account, comment=comment))

    def add_document(
        self,
        account: str,
        date: datetime.date,
        path: str,
        *,
        sha256: str | None = None,
    ) -> Document:
        """Attach a dated file reference to an account: a path and an
        optional SHA-256 of the content. The library stores the reference
        only; it never reads, copies, or verifies the file."""
        self._require_account_active(account, date)
        if not path.strip():
            raise DocumentError("a document needs a non-empty path")
        if sha256 is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                raise DocumentError(f"sha256 must be 64 hex digits, got {sha256!r}")
            sha256 = sha256.lower()
        return self._repo.add_document(
            Document(date=date, account=account, path=path, sha256=sha256)
        )

    def add_event(self, date: datetime.date, type: str, value: str) -> Event:
        """Record a dated fact about the ledger as a whole ('employer',
        'address', ...). The value may be empty; the type may not."""
        if not type.strip():
            raise EventError("an event needs a non-empty type")
        return self._repo.add_event(Event(date=date, type=type, value=value))

    def list_notes(self, account: str | None = None) -> list[Note]:
        return self._repo.list_notes(account)

    def list_documents(self, account: str | None = None) -> list[Document]:
        return self._repo.list_documents(account)

    def list_events(self, type: str | None = None) -> list[Event]:
        return self._repo.list_events(type)

    # --- validation --------------------------------------------------------

    def validate(self) -> ValidationReport:
        """Run every integrity check (design §12) and return a structured
        report. Read-only — stale assertion results are reported, not
        repaired (that is check_assertions())."""
        return _validate(self._repo)

    # --- reading -----------------------------------------------------------

    def get_transaction(self, transaction_id: int) -> Transaction | None:
        return self._repo.get_transaction(transaction_id)

    def list_transactions(
        self, *, tag: str | None = None, link: str | None = None
    ) -> list[Transaction]:
        """Every transaction in (date, id) order — or, with `tag` and/or
        `link`, only those carrying them. Tags slice across account
        boundaries: a #trip tag groups its transactions wherever they
        posted."""
        return self._repo.list_transactions(tag=tag, link=link)

    def balance(
        self,
        account: str,
        on: datetime.date | None = None,
        *,
        include_children: bool = True,
    ) -> Inventory:
        return _query_balance(
            self._repo, account, on, include_children=include_children
        )

    def inventory(
        self,
        account: str,
        on: datetime.date | None = None,
        *,
        include_children: bool = True,
    ) -> Inventory:
        """The account's holdings at end of day `on`, lot by lot: each
        surviving lot is a position at its cost; cost-less holdings (cash)
        are plain positions. `balance()` is this with costs collapsed."""
        return _query_inventory(
            self._repo, account, on, include_children=include_children
        )

    def list_lots(
        self,
        account: str | None = None,
        symbol: str | None = None,
        *,
        include_children: bool = False,
    ) -> list[Lot]:
        return self._repo.list_lots(account, symbol, include_children=include_children)

    # --- prices and valuation (design §9) ----------------------------------

    def record_price(
        self,
        commodity: str,
        date: datetime.date,
        price: Decimal,
        quote_commodity: str,
        *,
        origin: str = "directive",
    ) -> PricePoint:
        """Record that one unit of `commodity` was worth `price` of
        `quote_commodity` on `date`, replacing any existing row for that
        (date, commodity, quote) triple."""
        if origin not in PRICE_ORIGINS:
            raise OptionError(
                f"unknown price origin {origin!r}; known: {sorted(PRICE_ORIGINS)}"
            )
        self._require_commodity(commodity)
        quote = self._require_commodity(quote_commodity)
        if commodity == quote_commodity:
            raise InvalidPriceError(f"a price of {commodity} in itself is meaningless")
        if price <= 0:
            raise InvalidPriceError(f"prices must be positive, got {price}")
        return self._repo.add_price(
            PricePoint(
                date=date,
                commodity=commodity,
                price=Amount.from_decimal(price, quote),
                origin=origin,
            ),
            replace=True,
        )

    def get_price(
        self,
        commodity: str,
        quote_commodity: str,
        on: datetime.date | None = None,
    ) -> PricePoint | None:
        """The most recent price of `commodity` in `quote_commodity` at or
        before `on` (latest known when None), or None."""
        return self._repo.latest_price(commodity, quote_commodity, on)

    def list_prices(
        self,
        commodity: str | None = None,
        quote_commodity: str | None = None,
    ) -> list[PricePoint]:
        return self._repo.list_prices(commodity, quote_commodity)

    def _valuation_commodity(self, in_commodity: str | None) -> str:
        if in_commodity is not None:
            return in_commodity
        configured = self._repo.get_option("operating_currency")
        if configured is None:
            raise OptionError(
                "no valuation commodity: pass in_commodity or set the"
                " operating_currency option"
            )
        return configured

    def market_value(
        self,
        account: str,
        on: datetime.date | None = None,
        *,
        in_commodity: str | None = None,
    ) -> Amount:
        """The account's holdings (sub-accounts included) at end of day
        `on`, valued in `in_commodity` (default: the operating currency)
        via the price table's most-recent-at-or-before lookup."""
        return _query_market_value(
            self._repo,
            account,
            on,
            in_symbol=self._valuation_commodity(in_commodity),
        )

    def unrealized_gain(
        self,
        account: str,
        on: datetime.date | None = None,
        *,
        in_commodity: str | None = None,
    ) -> Amount:
        """Market value minus cost basis of the lots held under `account`
        at end of day `on` — computed from the price table, never posted
        (design §7)."""
        return _query_unrealized_gain(
            self._repo,
            account,
            on,
            in_symbol=self._valuation_commodity(in_commodity),
        )

    def journal(
        self,
        account: str,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
        *,
        include_children: bool = True,
        tag: str | None = None,
        link: str | None = None,
    ) -> list[JournalEntry]:
        return _query_journal(
            self._repo,
            account,
            start,
            end,
            include_children=include_children,
            tag=tag,
            link=link,
        )

    def balance_sheet(self, on: datetime.date | None = None) -> BalanceSheet:
        """Assets, Liabilities and Equity at end of day `on` (or over all
        time when None). Net worth is `balance_sheet(on).net_worth`."""
        return _query_balance_sheet(self._repo, on)

    def income_statement(
        self,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
    ) -> IncomeStatement:
        """Income and Expenses over [start, end], both inclusive, either
        side unbounded when None. The category breakdown drills down
        through each section's node tree."""
        return _query_income_statement(self._repo, start, end)

    # --- interop -----------------------------------------------------------

    def export_beancount_string(self) -> str:
        return _export_beancount_string(self._repo)

    def export_beancount(self, path: str | os.PathLike[str]) -> None:
        with open(path, "w", encoding="utf-8") as file:
            file.write(self.export_beancount_string())

    def import_beancount(self, path: str | os.PathLike[str]) -> ImportReport:
        """Replay a .beancount file into this (fresh) ledger — corpus test
        infrastructure (plan §8.1), not a product feature. Needs the
        dev-only `beancount` dependency, imported lazily so the runtime
        path stays free of it."""
        from ledger.interop.import_ import import_file

        return import_file(self, path)

    def import_beancount_string(self, text: str) -> ImportReport:
        from ledger.interop.import_ import import_string

        return import_string(self, text)
