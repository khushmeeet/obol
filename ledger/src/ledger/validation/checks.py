"""Individual invariant checks (design §12).

Each check takes already-loaded data (or the repository, where a balance
query is needed), returns a list of structured findings, and mutates
nothing — the validator is a corruption detector, not a repair tool.
Check 14 (checkpoints) from design §12 lands with its table.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ledger.domain.accounts import Account
from ledger.domain.amount import SCALE, scaled_product
from ledger.domain.directives import (
    AssertionStatus,
    BalanceAssertion,
    Pad,
    evaluate_assertion,
)
from ledger.domain.inventory import Lot, LotReductionEntry
from ledger.domain.transaction import (
    PAD_SOURCE,
    REVERSAL_SOURCE,
    Posting,
    Transaction,
)
from ledger.query.balances import balance_value_before

if TYPE_CHECKING:
    from ledger.storage.repositories import Repository


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    message: str
    transaction_id: int | None = None
    account: str | None = None
    assertion_id: int | None = None
    pad_id: int | None = None
    lot_id: int | None = None


def _is_acquisition(posting: Posting) -> bool:
    return (
        posting.cost is not None and posting.units.value > 0 and not posting.lot_matches
    )


def _transaction_tolerances(
    transaction: Transaction, multiplier: Decimal
) -> dict[str, Decimal]:
    """Per-commodity tolerances re-inferred from the stored postings —
    the same rule as recording (half the smallest written unit, loosest
    contribution wins), over the *written* (non-interpolated) postings."""
    tolerances: dict[str, Decimal] = {}
    for posting in transaction.postings:
        if posting.interpolated:
            continue
        precision = posting.units.precision
        if precision < 1:
            continue
        tolerance = Decimal(5).scaleb(-(precision + 1)) * multiplier
        symbol = posting.units.commodity.symbol
        current = tolerances.get(symbol)
        if current is None or tolerance > current:
            tolerances[symbol] = tolerance
    return tolerances


def check_transaction_balance(
    transactions: list[Transaction], multiplier: Decimal
) -> list[Finding]:
    """Design §12 check 1: per transaction, per commodity, weights sum to
    zero within the tolerance its own written precisions imply."""
    findings = []
    for transaction in transactions:
        residuals: dict[str, int] = {}
        for posting in transaction.postings:
            symbol = posting.weight.commodity.symbol
            residuals[symbol] = residuals.get(symbol, 0) + posting.weight.value
        tolerances = _transaction_tolerances(transaction, multiplier)
        for symbol, residual in sorted(residuals.items()):
            tolerance = tolerances.get(symbol, Decimal(0))
            if abs(Decimal(residual)) > tolerance.scaleb(SCALE):
                findings.append(
                    Finding(
                        check="transaction-balance",
                        message=(
                            f"transaction {transaction.id} does not balance in"
                            f" {symbol}: residual"
                            f" {Decimal(residual).scaleb(-SCALE).normalize()}"
                            f" exceeds tolerance {tolerance}"
                        ),
                        transaction_id=transaction.id,
                    )
                )
    return findings


def check_minimum_postings(transactions: list[Transaction]) -> list[Finding]:
    """Design §12 check 2: every transaction has at least two postings."""
    return [
        Finding(
            check="minimum-postings",
            message=(
                f"transaction {transaction.id} has"
                f" {len(transaction.postings)} posting(s); at least two required"
            ),
            transaction_id=transaction.id,
        )
        for transaction in transactions
        if len(transaction.postings) < 2
    ]


def check_single_interpolation(transactions: list[Transaction]) -> list[Finding]:
    """Design §12 check 3: at most one interpolated posting per commodity
    per transaction."""
    findings = []
    for transaction in transactions:
        counts: dict[str, int] = {}
        for posting in transaction.postings:
            if posting.interpolated:
                symbol = posting.units.commodity.symbol
                counts[symbol] = counts.get(symbol, 0) + 1
        for symbol, count in sorted(counts.items()):
            if count > 1:
                findings.append(
                    Finding(
                        check="interpolation",
                        message=(
                            f"transaction {transaction.id} has {count}"
                            f" interpolated postings in {symbol}; at most one allowed"
                        ),
                        transaction_id=transaction.id,
                    )
                )
    return findings


def check_account_lifetimes(
    transactions: list[Transaction], accounts: dict[str, Account]
) -> list[Finding]:
    """Design §12 check 4: no posting references an account outside its
    open/close window."""
    findings = []
    for transaction in transactions:
        for posting in transaction.postings:
            account = accounts.get(posting.account)
            if account is None:
                findings.append(
                    Finding(
                        check="account-lifetime",
                        message=(
                            f"transaction {transaction.id} posts to unknown"
                            f" account {posting.account!r}"
                        ),
                        transaction_id=transaction.id,
                        account=posting.account,
                    )
                )
            elif not account.is_open_on(transaction.date):
                findings.append(
                    Finding(
                        check="account-lifetime",
                        message=(
                            f"transaction {transaction.id} dated {transaction.date}"
                            f" posts to {posting.account!r}, open"
                            f" {account.opened_on}..{account.closed_on or 'present'}"
                        ),
                        transaction_id=transaction.id,
                        account=posting.account,
                    )
                )
    return findings


def check_allowed_commodities(
    transactions: list[Transaction], accounts: dict[str, Account]
) -> list[Finding]:
    """Design §12 check 5: no posting violates its account's
    allowed-commodity constraint."""
    findings = []
    for transaction in transactions:
        for posting in transaction.postings:
            account = accounts.get(posting.account)
            if account is None or account.allowed_commodities is None:
                continue
            symbol = posting.units.commodity.symbol
            if symbol not in account.allowed_commodities:
                findings.append(
                    Finding(
                        check="allowed-commodities",
                        message=(
                            f"transaction {transaction.id} posts {symbol} to"
                            f" {posting.account!r}, which allows only"
                            f" {sorted(account.allowed_commodities)}"
                        ),
                        transaction_id=transaction.id,
                        account=posting.account,
                    )
                )
    return findings


def check_weight_consistency(
    transactions: list[Transaction],
    lots_by_id: dict[int, Lot],
) -> list[Finding]:
    """Every stored weight must be recomputable from its posting: units
    for a plain posting, units × cost for an acquisition, units × price
    for a price-only posting, and −Σ(matched quantity × lot cost) for a
    booked reduction (or restoration). A weight that disagrees is
    corruption (a flipped or doctored column), not a rounding artifact —
    the recomputation uses the same half-even scale-8 products as
    record() (design §5)."""
    findings = []

    def bad(transaction: Transaction, posting: Posting, message: str) -> Finding:
        return Finding(
            check="weight-consistency",
            message=(
                f"transaction {transaction.id}: posting to"
                f" {posting.account!r} {message}"
            ),
            transaction_id=transaction.id,
            account=posting.account,
        )

    for transaction in transactions:
        for posting in transaction.postings:
            weight = posting.weight
            if posting.lot_matches:
                if posting.cost is None:
                    findings.append(
                        bad(transaction, posting, "has lot matches but no cost")
                    )
                    continue
                expected = 0
                commodity = None
                dangling = False
                for match in posting.lot_matches:
                    lot = lots_by_id.get(match.lot_id)
                    if lot is None:
                        findings.append(
                            bad(
                                transaction,
                                posting,
                                f"references lot {match.lot_id}, which does not exist",
                            )
                        )
                        dangling = True
                        break
                    expected -= scaled_product(match.quantity, lot.cost.value)
                    commodity = lot.cost.commodity
                if dangling:
                    continue
                if weight.value != expected or weight.commodity != commodity:
                    findings.append(
                        bad(
                            transaction,
                            posting,
                            f"has weight {weight!r}; its lot matches imply"
                            f" {Decimal(expected).scaleb(-SCALE).normalize()}"
                            f" {commodity.symbol if commodity else '?'}",
                        )
                    )
            elif posting.cost is not None:
                if posting.units.value < 0:
                    findings.append(
                        bad(
                            transaction,
                            posting,
                            "is a cost reduction with no lot matches"
                            " (unbooked reduction)",
                        )
                    )
                    continue
                if posting.cost.per_unit is None:
                    findings.append(
                        bad(
                            transaction,
                            posting,
                            "is an acquisition without a per-unit cost",
                        )
                    )
                    continue
                expected = scaled_product(
                    posting.units.value, posting.cost.per_unit.value
                )
                if (
                    weight.value != expected
                    or weight.commodity != posting.cost.per_unit.commodity
                ):
                    findings.append(
                        bad(
                            transaction,
                            posting,
                            f"has weight {weight!r} != units × cost"
                            f" ({Decimal(expected).scaleb(-SCALE).normalize()}"
                            f" {posting.cost.per_unit.commodity.symbol})",
                        )
                    )
            elif posting.price is not None:
                expected = scaled_product(posting.units.value, posting.price.value)
                if (
                    weight.value != expected
                    or weight.commodity != posting.price.commodity
                ):
                    findings.append(
                        bad(
                            transaction,
                            posting,
                            f"has weight {weight!r} != units × price"
                            f" ({Decimal(expected).scaleb(-SCALE).normalize()}"
                            f" {posting.price.commodity.symbol})",
                        )
                    )
            elif (
                weight.value != posting.units.value
                or weight.commodity != posting.units.commodity
            ):
                findings.append(
                    bad(
                        transaction,
                        posting,
                        f"has weight {weight!r} != units {posting.units!r}",
                    )
                )
    return findings


def check_lots(
    transactions: list[Transaction],
    lots: list[Lot],
    reductions: list[LotReductionEntry],
) -> list[Finding]:
    """Design §12 checks 7–9: reductions never exceed a lot's original
    quantity, remaining quantity never goes negative (or above the
    original) at any point in entry order — the order booking actually
    happened in — and every reduction is booked against a lot in its own
    account and commodity. Plus the trace both ways: every lot to a real
    acquisition posting that agrees with it, every acquisition posting to
    exactly one lot, every match-carrying posting internally consistent,
    and no reduction dated before its lot's opening transaction."""
    findings: list[Finding] = []
    transactions_by_id = {t.id: t for t in transactions if t.id is not None}
    lots_by_id = {lot.id: lot for lot in lots if lot.id is not None}
    lots_by_opening = {
        (lot.opened_by_transaction_id, lot.opened_by_seq): lot for lot in lots
    }

    def posting_at(transaction_id: int, seq: int) -> Posting | None:
        transaction = transactions_by_id.get(transaction_id)
        if transaction is None or not 0 <= seq < len(transaction.postings):
            return None
        return transaction.postings[seq]

    for lot in lots:
        assert lot.opened_by_transaction_id is not None
        assert lot.opened_by_seq is not None
        opening = posting_at(lot.opened_by_transaction_id, lot.opened_by_seq)
        if opening is None:
            findings.append(
                Finding(
                    check="lot-trace",
                    message=(
                        f"lot {lot.id} claims opening posting"
                        f" (transaction {lot.opened_by_transaction_id},"
                        f" seq {lot.opened_by_seq}), which does not exist"
                    ),
                    lot_id=lot.id,
                )
            )
        elif (
            not _is_acquisition(opening)
            or opening.account != lot.account
            or opening.units.commodity != lot.commodity
            or opening.units.value != lot.original_quantity
            or opening.cost is None
            or opening.cost.per_unit != lot.cost
            or opening.cost.date != lot.acquired_on
            or opening.cost.label != lot.label
        ):
            findings.append(
                Finding(
                    check="lot-trace",
                    message=(
                        f"lot {lot.id} disagrees with its opening posting"
                        f" (transaction {lot.opened_by_transaction_id},"
                        f" seq {lot.opened_by_seq}): expected an acquisition"
                        f" of {lot.original_quantity} {lot.commodity.symbol}"
                        f" at {lot.cost!r} on {lot.acquired_on}"
                    ),
                    lot_id=lot.id,
                    transaction_id=lot.opened_by_transaction_id,
                    account=lot.account,
                )
            )

    for transaction in transactions:
        for seq, posting in enumerate(transaction.postings):
            if _is_acquisition(posting):
                if (transaction.id, seq) not in lots_by_opening:
                    findings.append(
                        Finding(
                            check="lot-trace",
                            message=(
                                f"transaction {transaction.id}: acquisition of"
                                f" {posting.units!r} at cost opened no lot"
                            ),
                            transaction_id=transaction.id,
                            account=posting.account,
                        )
                    )
            if posting.lot_matches:
                total = sum(match.quantity for match in posting.lot_matches)
                if total != -posting.units.value:
                    findings.append(
                        Finding(
                            check="lot-trace",
                            message=(
                                f"transaction {transaction.id}: posting of"
                                f" {posting.units!r} to {posting.account!r} has"
                                f" lot matches totalling {total}, expected"
                                f" {-posting.units.value}"
                            ),
                            transaction_id=transaction.id,
                            account=posting.account,
                        )
                    )

    running: dict[int, int] = {lot_id: 0 for lot_id in lots_by_id}
    for entry in reductions:
        lot = lots_by_id.get(entry.lot_id)
        if lot is None:
            findings.append(
                Finding(
                    check="lot-reduction",
                    message=(
                        f"reduction {entry.id} references lot {entry.lot_id},"
                        f" which does not exist"
                    ),
                    transaction_id=entry.transaction_id,
                )
            )
            continue
        posting = posting_at(entry.transaction_id, entry.seq)
        if posting is None:
            findings.append(
                Finding(
                    check="lot-reduction",
                    message=(
                        f"reduction {entry.id} of lot {lot.id} claims posting"
                        f" (transaction {entry.transaction_id}, seq {entry.seq}),"
                        f" which does not exist"
                    ),
                    lot_id=lot.id,
                )
            )
            continue
        if posting.account != lot.account or posting.units.commodity != lot.commodity:
            # Design §12 check 9.
            findings.append(
                Finding(
                    check="lot-reduction",
                    message=(
                        f"reduction {entry.id}: transaction"
                        f" {entry.transaction_id} posts to {posting.account!r}"
                        f" in {posting.units.commodity.symbol} but draws on"
                        f" lot {lot.id} of {lot.commodity.symbol}"
                        f" in {lot.account!r}"
                    ),
                    lot_id=lot.id,
                    transaction_id=entry.transaction_id,
                    account=posting.account,
                )
            )
        if lot.recorded_on is not None and entry.date < lot.recorded_on:
            findings.append(
                Finding(
                    check="lot-reduction",
                    message=(
                        f"reduction {entry.id} dated {entry.date} predates"
                        f" lot {lot.id}, recorded on {lot.recorded_on}"
                    ),
                    lot_id=lot.id,
                    transaction_id=entry.transaction_id,
                )
            )
        running[entry.lot_id] += entry.quantity
        if not 0 <= running[entry.lot_id] <= lot.original_quantity:
            # Design §12 checks 7–8, in entry order: consumption may never
            # exceed the original quantity nor be restored below zero.
            findings.append(
                Finding(
                    check="lot-reduction",
                    message=(
                        f"lot {lot.id} reaches consumed quantity"
                        f" {running[entry.lot_id]} of original"
                        f" {lot.original_quantity} at reduction {entry.id}"
                    ),
                    lot_id=lot.id,
                    transaction_id=entry.transaction_id,
                )
            )
    return findings


def check_global_balance(
    transactions: list[Transaction], multiplier: Decimal
) -> list[Finding]:
    """Design §12 check 10: the sum of all weights across the entire
    ledger is zero per commodity — no money invented or destroyed. The
    allowance is the accumulated per-transaction tolerance budget: a
    transaction recorded with a residual inside its own tolerance keeps
    that slack forever (interpolated fills are exact, so the budget is
    zero slack in the common case)."""
    totals: dict[str, int] = {}
    budgets: dict[str, Decimal] = {}
    for transaction in transactions:
        for posting in transaction.postings:
            symbol = posting.weight.commodity.symbol
            totals[symbol] = totals.get(symbol, 0) + posting.weight.value
        for symbol, tolerance in _transaction_tolerances(
            transaction, multiplier
        ).items():
            budgets[symbol] = budgets.get(symbol, Decimal(0)) + tolerance
    return [
        Finding(
            check="global-balance",
            message=(
                f"ledger-wide weights sum to"
                f" {Decimal(total).scaleb(-SCALE).normalize()} {symbol},"
                f" outside the accumulated tolerance budget"
                f" {budgets.get(symbol, Decimal(0))}"
            ),
        )
        for symbol, total in sorted(totals.items())
        if abs(Decimal(total)) > budgets.get(symbol, Decimal(0)).scaleb(SCALE)
    ]


def check_assertions(
    repository: Repository,
    assertions: list[BalanceAssertion],
    multiplier: Decimal,
) -> list[Finding]:
    """Design §12 check 6: every balance assertion matches the computed
    balance at the start of its date (sub-accounts included) within its
    own precision-derived tolerance — and its stored outcome matches a
    fresh recomputation (a stale status is itself a finding)."""
    findings = []
    for assertion in assertions:
        assert assertion.id is not None
        actual = balance_value_before(
            repository,
            assertion.account,
            assertion.date,
            assertion.amount.commodity.symbol,
        )
        status, difference = evaluate_assertion(assertion.amount, actual, multiplier)
        if status is AssertionStatus.FAIL:
            findings.append(
                Finding(
                    check="balance-assertion",
                    message=(
                        f"assertion {assertion.id} fails: {assertion.account!r}"
                        f" asserted {assertion.amount!r} at start of"
                        f" {assertion.date}, computed"
                        f" {Decimal(actual).scaleb(-SCALE).normalize()}"
                        f" (difference {difference.to_decimal()})"
                    ),
                    assertion_id=assertion.id,
                    account=assertion.account,
                )
            )
        stale = assertion.status is not AssertionStatus.UNCHECKED and (
            assertion.status is not status
            or assertion.difference is None
            or assertion.difference.value != difference.value
        )
        if stale:
            findings.append(
                Finding(
                    check="balance-assertion",
                    message=(
                        f"assertion {assertion.id} carries stale results:"
                        f" stored {assertion.status.value}"
                        f" (difference {assertion.difference!r}), recomputed"
                        f" {status.value} (difference {difference!r});"
                        f" run check_assertions()"
                    ),
                    assertion_id=assertion.id,
                    account=assertion.account,
                )
            )
    return findings


def check_generated_trace(
    transactions: list[Transaction],
    pads: list[Pad],
    assertions: list[BalanceAssertion],
) -> list[Finding]:
    """Design §12 check 13: every generated transaction traces to its
    originating directive, and every directive that claims a generated
    transaction points at a real, matching one."""
    findings = []
    transactions_by_id = {t.id: t for t in transactions}
    pads_by_id = {p.id: p for p in pads}
    assertion_ids = {a.id for a in assertions}

    for transaction in transactions:
        if not transaction.generated:
            continue
        if transaction.source != PAD_SOURCE:
            findings.append(
                Finding(
                    check="generated-trace",
                    message=(
                        f"generated transaction {transaction.id} has source"
                        f" {transaction.source!r}; only 'pad' generates"
                        f" whole transactions (a sale's gain is an"
                        f" interpolated posting, not a generated transaction)"
                    ),
                    transaction_id=transaction.id,
                )
            )
            continue
        pad = None
        if transaction.source_ref is not None and transaction.source_ref.isdigit():
            pad = pads_by_id.get(int(transaction.source_ref))
        if pad is None:
            findings.append(
                Finding(
                    check="generated-trace",
                    message=(
                        f"generated transaction {transaction.id} references"
                        f" pad {transaction.source_ref!r}, which does not exist"
                    ),
                    transaction_id=transaction.id,
                )
            )
        elif pad.generated_txn_id != transaction.id:
            findings.append(
                Finding(
                    check="generated-trace",
                    message=(
                        f"generated transaction {transaction.id} references"
                        f" pad {pad.id}, but that pad records"
                        f" generated_txn {pad.generated_txn_id}"
                    ),
                    transaction_id=transaction.id,
                    pad_id=pad.id,
                )
            )
        elif {p.account for p in transaction.postings} != {
            pad.account,
            pad.source_account,
        } or transaction.date != pad.date:
            findings.append(
                Finding(
                    check="generated-trace",
                    message=(
                        f"padding transaction {transaction.id} does not match"
                        f" pad {pad.id}: expected postings between"
                        f" {pad.account!r} and {pad.source_account!r}"
                        f" dated {pad.date}"
                    ),
                    transaction_id=transaction.id,
                    pad_id=pad.id,
                )
            )

    for pad in pads:
        if pad.generated_txn_id is not None:
            transaction = transactions_by_id.get(pad.generated_txn_id)
            if transaction is None or not transaction.generated:
                findings.append(
                    Finding(
                        check="generated-trace",
                        message=(
                            f"pad {pad.id} records generated transaction"
                            f" {pad.generated_txn_id}, which is missing or"
                            f" not marked generated"
                        ),
                        pad_id=pad.id,
                    )
                )
            if pad.consumed_by is None:
                findings.append(
                    Finding(
                        check="generated-trace",
                        message=(
                            f"pad {pad.id} generated transaction"
                            f" {pad.generated_txn_id} without being consumed"
                            f" by an assertion"
                        ),
                        pad_id=pad.id,
                    )
                )
        if pad.consumed_by is not None and pad.consumed_by not in assertion_ids:
            findings.append(
                Finding(
                    check="generated-trace",
                    message=(
                        f"pad {pad.id} was consumed by assertion"
                        f" {pad.consumed_by}, which does not exist"
                    ),
                    pad_id=pad.id,
                )
            )
    return findings


def _cost_price_key(
    posting: Posting,
) -> tuple[tuple[object, ...] | None, tuple[object, ...] | None]:
    cost = posting.cost
    cost_key = None
    if cost is not None:
        cost_key = (
            cost.per_unit.value if cost.per_unit is not None else None,
            cost.cost_commodity,
            cost.date,
            cost.label,
        )
    price_key = None
    if posting.price is not None:
        price_key = (posting.price.value, posting.price.commodity.symbol)
    return cost_key, price_key


def check_reversals(
    transactions: list[Transaction],
    lots_by_opening: dict[tuple[int | None, int | None], Lot] | None = None,
) -> list[Finding]:
    """Design §11 as a check: every reversal exactly negates an existing,
    non-generated original, is dated no earlier than it, and is the only
    reversal of that original; every replacement (reverses_id without the
    reversal source) corrects an original that was actually reversed —
    otherwise its effect is double-counted. Negation includes lots: a
    reduction's matches come back negated, an acquisition comes back as a
    full consumption of the lot it opened."""
    findings = []
    lots_by_opening = lots_by_opening or {}
    transactions_by_id = {t.id: t for t in transactions}
    reversal_ids_of: dict[int, list[int | None]] = {}
    for transaction in transactions:
        if (
            transaction.source == REVERSAL_SOURCE
            and transaction.reverses_id is not None
        ):
            reversal_ids_of.setdefault(transaction.reverses_id, []).append(
                transaction.id
            )

    def finding(transaction: Transaction, message: str) -> Finding:
        return Finding(
            check="reversal-trace",
            message=message,
            transaction_id=transaction.id,
        )

    for transaction in transactions:
        is_reversal = transaction.source == REVERSAL_SOURCE
        if transaction.reverses_id is None:
            if is_reversal:
                findings.append(
                    finding(
                        transaction,
                        f"reversal {transaction.id} carries no reverses_id",
                    )
                )
            continue
        original = transactions_by_id.get(transaction.reverses_id)
        if original is None:
            findings.append(
                finding(
                    transaction,
                    f"transaction {transaction.id} corrects transaction"
                    f" {transaction.reverses_id}, which does not exist",
                )
            )
            continue
        if not is_reversal:
            # A replacement: valid only alongside the reversal that
            # cancels the original it supersedes.
            if not reversal_ids_of.get(original.id):
                findings.append(
                    finding(
                        transaction,
                        f"replacement {transaction.id} corrects transaction"
                        f" {original.id}, which has no reversal — its effect"
                        f" is double-counted",
                    )
                )
            continue
        if transaction.source_ref != str(transaction.reverses_id):
            findings.append(
                finding(
                    transaction,
                    f"reversal {transaction.id} reverses transaction"
                    f" {transaction.reverses_id} but carries source_ref"
                    f" {transaction.source_ref!r}",
                )
            )
        if original.source == REVERSAL_SOURCE:
            findings.append(
                finding(
                    transaction,
                    f"reversal {transaction.id} reverses transaction"
                    f" {original.id}, which is itself a reversal",
                )
            )
        if original.generated:
            findings.append(
                finding(
                    transaction,
                    f"reversal {transaction.id} reverses transaction"
                    f" {original.id}, which is machine-generated",
                )
            )
        if transaction.date < original.date:
            findings.append(
                finding(
                    transaction,
                    f"reversal {transaction.id} dated {transaction.date}"
                    f" predates transaction {original.id} dated"
                    f" {original.date}; corrections are never backdated",
                )
            )
        negated = [
            (
                p.account,
                p.units.commodity.symbol,
                -p.units.value,
                -p.weight.value,
                *_cost_price_key(p),
            )
            for p in original.postings
        ]
        actual = [
            (
                p.account,
                p.units.commodity.symbol,
                p.units.value,
                p.weight.value,
                *_cost_price_key(p),
            )
            for p in transaction.postings
        ]
        if actual != negated:
            findings.append(
                finding(
                    transaction,
                    f"reversal {transaction.id} does not exactly negate"
                    f" transaction {original.id}, posting for posting",
                )
            )
        else:
            for seq, (orig, rev) in enumerate(
                zip(original.postings, transaction.postings, strict=True)
            ):
                if orig.lot_matches:
                    expected = tuple((m.lot_id, -m.quantity) for m in orig.lot_matches)
                elif _is_acquisition(orig):
                    lot = lots_by_opening.get((original.id, seq))
                    expected = ((lot.id, orig.units.value),) if lot is not None else ()
                else:
                    expected = ()
                if tuple((m.lot_id, m.quantity) for m in rev.lot_matches) != expected:
                    findings.append(
                        finding(
                            transaction,
                            f"reversal {transaction.id} does not undo the lot"
                            f" effects of transaction {original.id} (posting"
                            f" {seq}): expected matches {expected}",
                        )
                    )

    for original_id, reversal_ids in sorted(reversal_ids_of.items()):
        if len(reversal_ids) > 1:
            findings.append(
                Finding(
                    check="reversal-trace",
                    message=(
                        f"transaction {original_id} has {len(reversal_ids)}"
                        f" reversals ({reversal_ids}); at most one allowed"
                    ),
                    transaction_id=original_id,
                )
            )
    return findings


def check_storage(repository: Repository) -> list[Finding]:
    """Design §12 checks 11–12, backend-specific: orphan rows, dangling
    references, and parent-link mismatches only the storage layer can see."""
    return [
        Finding(check="storage-integrity", message=message)
        for message in repository.storage_integrity()
    ]
