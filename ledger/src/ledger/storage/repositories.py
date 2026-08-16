"""Row <-> domain mapping, behind a Repository protocol.

Two implementations share one contract and one test suite: SQLite (the
real backend) and in-memory (keeps domain tests fast and the domain layer
honest about not needing SQL). Disagreement between the two is a bug in
the SQLite layer.

No update or delete path exists for transactions or postings — corrections
are new transactions (design §11).
"""

import dataclasses
import datetime
import json
import sqlite3
from collections.abc import Collection, Mapping
from typing import Any, Protocol

from ledger.domain.accounts import Account, AccountType, parent_path
from ledger.domain.amount import SCALE, Amount, Commodity, CommodityKind
from ledger.domain.booking import BookingMethod, LotMatch
from ledger.domain.directives import (
    AssertionStatus,
    BalanceAssertion,
    Document,
    Event,
    Note,
    Pad,
    PricePoint,
)
from ledger.domain.errors import (
    AccountError,
    DuplicateAccountError,
    DuplicateCommodityError,
    DuplicateSourceError,
    UnknownAccountError,
    UnknownCommodityError,
)
from ledger.domain.inventory import Cost, Lot, LotReductionEntry
from ledger.domain.transaction import Posting, Transaction
from ledger.query.journal import JournalEntry
from ledger.storage.db import unit_of_work


class Repository(Protocol):
    # commodities
    def add_commodity(
        self,
        commodity: Commodity,
        *,
        name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None: ...
    def get_commodity(self, symbol: str) -> Commodity | None: ...
    def list_commodities(self) -> list[Commodity]: ...

    # accounts
    def add_account(self, account: Account) -> None: ...
    def get_account(self, path: str) -> Account | None: ...
    def list_accounts(self) -> list[Account]: ...
    def close_account(self, path: str, closed_on: datetime.date) -> None: ...

    # options
    def set_option(self, key: str, value: str) -> None: ...
    def get_option(self, key: str) -> str | None: ...
    def list_options(self) -> dict[str, str]: ...

    # transactions
    def add_transaction(self, transaction: Transaction) -> Transaction: ...
    def get_transaction(self, transaction_id: int) -> Transaction | None: ...
    def get_transaction_by_source(
        self, source: str | None, source_ref: str
    ) -> Transaction | None: ...
    def list_transactions(
        self, *, tag: str | None = None, link: str | None = None
    ) -> list[Transaction]: ...

    # lots (M7) — created and reduced only through add_transaction; these
    # are the read paths. Reductions are returned in creation order, the
    # order booking actually happened in (entry order, not date order).
    def list_lots(
        self,
        account: str | None = None,
        symbol: str | None = None,
        *,
        include_children: bool = False,
    ) -> list[Lot]: ...
    def get_lot_by_opening(self, transaction_id: int, seq: int) -> Lot | None: ...
    def list_lot_reductions(
        self, lot_ids: Collection[int] | None = None
    ) -> list[LotReductionEntry]: ...

    # prices (M7)
    def add_price(self, price: PricePoint, *, replace: bool = False) -> PricePoint: ...
    def list_prices(
        self, symbol: str | None = None, quote: str | None = None
    ) -> list[PricePoint]: ...
    def latest_price(
        self, symbol: str, quote: str, on: datetime.date | None = None
    ) -> PricePoint | None: ...

    # hub attachments (M6)
    def add_note(self, note: Note) -> Note: ...
    def list_notes(self, account: str | None = None) -> list[Note]: ...
    def add_document(self, document: Document) -> Document: ...
    def list_documents(self, account: str | None = None) -> list[Document]: ...
    def add_event(self, event: Event) -> Event: ...
    def list_events(self, type: str | None = None) -> list[Event]: ...

    # balance assertions (M4)
    def add_assertion(self, assertion: BalanceAssertion) -> BalanceAssertion: ...
    def get_assertion(self, assertion_id: int) -> BalanceAssertion | None: ...
    def list_assertions(self, account: str | None = None) -> list[BalanceAssertion]: ...
    def update_assertion_check(
        self,
        assertion_id: int,
        status: AssertionStatus,
        difference: int,
        checked_at: datetime.datetime,
    ) -> None: ...

    # pads (M4)
    def add_pad(self, pad: Pad) -> Pad: ...
    def get_pad(self, pad_id: int) -> Pad | None: ...
    def list_pads(self, account: str | None = None) -> list[Pad]: ...
    def consume_pad(
        self,
        pad_id: int,
        assertion_id: int,
        transaction_id: int | None,
    ) -> None: ...

    # storage-level integrity (validation check support). Semantic checks
    # live in validation/checks.py over this protocol; corruption that only
    # a backend can see (dangling foreign keys, orphan rows, parent-link
    # mismatches) is reported here as plain messages.
    def storage_integrity(self) -> list[str]: ...

    # queries
    def journal(
        self,
        account: str,
        *,
        include_children: bool = True,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
        tag: str | None = None,
        link: str | None = None,
    ) -> list[JournalEntry]: ...
    def balance(
        self,
        account: str,
        *,
        include_children: bool = True,
        on: datetime.date | None = None,
    ) -> dict[str, int]: ...
    def balances_by_account(
        self,
        *,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
    ) -> dict[str, dict[str, int]]: ...


def _dump_metadata(metadata: Mapping[str, object] | None) -> str | None:
    if not metadata:
        return None
    return json.dumps(dict(metadata), sort_keys=True)


def _load_metadata(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    loaded: dict[str, Any] = json.loads(raw)
    return loaded


def _normalize_metadata(metadata: Mapping[str, object]) -> dict[str, Any]:
    """JSON round-trip, so both backends enforce (and normalize to) exactly
    what SQLite storage can hold."""
    return _load_metadata(_dump_metadata(metadata)) or {}


def _account_matches(path: str, account: str, include_children: bool) -> bool:
    if path == account:
        return True
    return include_children and path.startswith(account + ":")


class SQLiteRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def _cursor(self) -> sqlite3.Cursor:
        # Row factory set per-cursor so borrowed connections keep their own.
        cursor = self._conn.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor

    # --- commodities -------------------------------------------------------

    def add_commodity(
        self,
        commodity: Commodity,
        *,
        name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        try:
            with unit_of_work(self._conn):
                self._conn.execute(
                    "INSERT INTO commodities"
                    " (symbol, name, kind, display_precision, metadata)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        commodity.symbol,
                        name,
                        commodity.kind.value,
                        commodity.display_precision,
                        _dump_metadata(metadata),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateCommodityError(
                f"commodity {commodity.symbol!r} already exists"
            ) from exc

    def get_commodity(self, symbol: str) -> Commodity | None:
        row = (
            self._cursor()
            .execute(
                "SELECT symbol, kind, display_precision FROM commodities"
                " WHERE symbol = ?",
                (symbol,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Commodity(
            symbol=row["symbol"],
            kind=CommodityKind(row["kind"]),
            display_precision=row["display_precision"],
        )

    def list_commodities(self) -> list[Commodity]:
        rows = (
            self._cursor()
            .execute(
                "SELECT symbol, kind, display_precision FROM commodities"
                " ORDER BY symbol"
            )
            .fetchall()
        )
        return [
            Commodity(
                symbol=row["symbol"],
                kind=CommodityKind(row["kind"]),
                display_precision=row["display_precision"],
            )
            for row in rows
        ]

    def _commodity_id(self, symbol: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM commodities WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            raise UnknownCommodityError(symbol)
        return int(row[0])

    # --- accounts ----------------------------------------------------------

    def add_account(self, account: Account) -> None:
        parent = parent_path(account.path)
        parent_id = None
        if parent is not None:
            row = self._conn.execute(
                "SELECT id FROM accounts WHERE path = ?", (parent,)
            ).fetchone()
            parent_id = row[0] if row else None
        allowed = (
            None
            if account.allowed_commodities is None
            else json.dumps(sorted(account.allowed_commodities))
        )
        try:
            with unit_of_work(self._conn):
                self._conn.execute(
                    "INSERT INTO accounts"
                    " (path, type, parent_id, opened_on, closed_on,"
                    "  booking_method, allowed_commodities, metadata)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        account.path,
                        account.type.value,
                        parent_id,
                        account.opened_on.isoformat(),
                        account.closed_on.isoformat() if account.closed_on else None,
                        account.booking_method.value,
                        allowed,
                        _dump_metadata(account.metadata),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccountError(
                f"account {account.path!r} already exists"
            ) from exc

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        allowed = row["allowed_commodities"]
        return Account(
            path=row["path"],
            type=AccountType(row["type"]),
            opened_on=datetime.date.fromisoformat(row["opened_on"]),
            closed_on=datetime.date.fromisoformat(row["closed_on"])
            if row["closed_on"]
            else None,
            booking_method=BookingMethod(row["booking_method"]),
            allowed_commodities=frozenset(json.loads(allowed)) if allowed else None,
            metadata=_load_metadata(row["metadata"]),
        )

    def get_account(self, path: str) -> Account | None:
        row = (
            self._cursor()
            .execute("SELECT * FROM accounts WHERE path = ?", (path,))
            .fetchone()
        )
        return self._row_to_account(row) if row else None

    def list_accounts(self) -> list[Account]:
        rows = self._cursor().execute("SELECT * FROM accounts ORDER BY path").fetchall()
        return [self._row_to_account(row) for row in rows]

    def close_account(self, path: str, closed_on: datetime.date) -> None:
        account = self.get_account(path)
        if account is None:
            raise UnknownAccountError(path)
        if account.closed_on is not None:
            raise AccountError(
                f"account {path!r} is already closed on {account.closed_on}"
            )
        if closed_on < account.opened_on:
            raise AccountError(
                f"cannot close {path!r} on {closed_on}, "
                f"before it opened on {account.opened_on}"
            )
        with unit_of_work(self._conn):
            self._conn.execute(
                "UPDATE accounts SET closed_on = ? WHERE path = ?",
                (closed_on.isoformat(), path),
            )

    def _account_id(self, path: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM accounts WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            raise UnknownAccountError(path)
        return int(row[0])

    # --- options -----------------------------------------------------------

    def set_option(self, key: str, value: str) -> None:
        with unit_of_work(self._conn):
            self._conn.execute(
                "INSERT INTO ledger_options (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_option(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM ledger_options WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def list_options(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM ledger_options").fetchall()
        return {row[0]: row[1] for row in rows}

    # --- transactions ------------------------------------------------------

    def add_transaction(self, transaction: Transaction) -> Transaction:
        try:
            with unit_of_work(self._conn):
                if transaction.source_ref is not None:
                    row = self._conn.execute(
                        "SELECT id FROM transactions"
                        " WHERE source IS ? AND source_ref = ?",
                        (transaction.source, transaction.source_ref),
                    ).fetchone()
                    if row is not None:
                        raise DuplicateSourceError(
                            f"transaction with source="
                            f"{transaction.source!r}, source_ref="
                            f"{transaction.source_ref!r} already exists"
                            f" (id {row[0]})"
                        )
                cursor = self._conn.execute(
                    "INSERT INTO transactions"
                    " (date, flag, payee, narration, source, source_ref,"
                    "  reverses_id, generated, created_at, metadata)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        transaction.date.isoformat(),
                        transaction.flag,
                        transaction.payee,
                        transaction.narration,
                        transaction.source,
                        transaction.source_ref,
                        transaction.reverses_id,
                        1 if transaction.generated else 0,
                        (
                            transaction.created_at
                            or datetime.datetime.now(datetime.UTC)
                        ).isoformat(),
                        _dump_metadata(transaction.metadata),
                    ),
                )
                transaction_id = cursor.lastrowid
                assert transaction_id is not None
                for seq, posting in enumerate(transaction.postings):
                    cost = posting.cost
                    cost_commodity_id = None
                    if cost is not None and cost.cost_commodity is not None:
                        cost_commodity_id = self._commodity_id(cost.cost_commodity)
                    price = posting.price
                    posting_cursor = self._conn.execute(
                        "INSERT INTO postings"
                        " (transaction_id, account_id, seq, units,"
                        "  units_precision, commodity_id,"
                        "  cost_per_unit, cost_precision, cost_commodity,"
                        "  cost_date, cost_label,"
                        "  price_per_unit, price_precision, price_commodity,"
                        "  weight, weight_commodity, flag, interpolated,"
                        "  metadata)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                        "  ?, ?, ?, ?, ?)",
                        (
                            transaction_id,
                            self._account_id(posting.account),
                            seq,
                            posting.units.value,
                            posting.units.precision,
                            self._commodity_id(posting.units.commodity.symbol),
                            cost.per_unit.value
                            if cost is not None and cost.per_unit is not None
                            else None,
                            cost.per_unit.precision
                            if cost is not None and cost.per_unit is not None
                            else None,
                            cost_commodity_id,
                            cost.date.isoformat()
                            if cost is not None and cost.date is not None
                            else None,
                            cost.label if cost is not None else None,
                            price.value if price is not None else None,
                            price.precision if price is not None else None,
                            self._commodity_id(price.commodity.symbol)
                            if price is not None
                            else None,
                            posting.weight.value,
                            self._commodity_id(posting.weight.commodity.symbol),
                            posting.flag,
                            1 if posting.interpolated else 0,
                            _dump_metadata(posting.metadata),
                        ),
                    )
                    posting_id = posting_cursor.lastrowid
                    assert posting_id is not None
                    if (
                        cost is not None
                        and posting.units.value > 0
                        and not posting.lot_matches
                    ):
                        # An acquisition opens its lot (design §7); the
                        # resolver guarantees the cost is fully specified.
                        assert cost.per_unit is not None and cost.date is not None
                        self._conn.execute(
                            "INSERT INTO lots"
                            " (account_id, commodity_id, acquired_on,"
                            "  original_quantity, cost_per_unit,"
                            "  cost_precision, cost_commodity, label,"
                            "  opened_by_posting)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                self._account_id(posting.account),
                                self._commodity_id(posting.units.commodity.symbol),
                                cost.date.isoformat(),
                                posting.units.value,
                                cost.per_unit.value,
                                cost.per_unit.precision,
                                cost_commodity_id,
                                cost.label,
                                posting_id,
                            ),
                        )
                    for match in posting.lot_matches:
                        self._conn.execute(
                            "INSERT INTO lot_reductions"
                            " (lot_id, posting_id, quantity) VALUES (?, ?, ?)",
                            (match.lot_id, posting_id, match.quantity),
                        )
                for name in sorted(transaction.tags):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
                    )
                    self._conn.execute(
                        "INSERT INTO transaction_tags (transaction_id, tag_id)"
                        " SELECT ?, id FROM tags WHERE name = ?",
                        (transaction_id, name),
                    )
                for name in sorted(transaction.links):
                    self._conn.execute(
                        "INSERT INTO links (transaction_id, name) VALUES (?, ?)",
                        (transaction_id, name),
                    )
        except sqlite3.IntegrityError as exc:
            raise DuplicateSourceError(str(exc)) from exc
        return dataclasses.replace(transaction, id=transaction_id)

    def _commodity_map(self) -> dict[int, Commodity]:
        rows = self._conn.execute(
            "SELECT id, symbol, kind, display_precision FROM commodities"
        ).fetchall()
        return {
            row[0]: Commodity(
                symbol=row[1],
                kind=CommodityKind(row[2]),
                display_precision=row[3],
            )
            for row in rows
        }

    def _matches_by_posting(self) -> dict[int, tuple[LotMatch, ...]]:
        matches: dict[int, list[LotMatch]] = {}
        for posting_id, lot_id, quantity in self._conn.execute(
            "SELECT posting_id, lot_id, quantity FROM lot_reductions ORDER BY id"
        ):
            matches.setdefault(posting_id, []).append(
                LotMatch(lot_id=lot_id, quantity=quantity)
            )
        return {posting_id: tuple(found) for posting_id, found in matches.items()}

    def get_transaction(self, transaction_id: int) -> Transaction | None:
        row = (
            self._cursor()
            .execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,))
            .fetchone()
        )
        if row is None:
            return None
        posting_rows = (
            self._cursor()
            .execute(
                "SELECT p.*, a.path AS account_path"
                " FROM postings p"
                " JOIN accounts a ON a.id = p.account_id"
                " WHERE p.transaction_id = ?"
                " ORDER BY p.seq",
                (transaction_id,),
            )
            .fetchall()
        )
        commodities = self._commodity_map()
        matches = self._matches_by_posting()
        postings = tuple(
            self._row_to_posting(p, commodities, matches) for p in posting_rows
        )
        tags = frozenset(
            r[0]
            for r in self._conn.execute(
                "SELECT g.name FROM transaction_tags tt"
                " JOIN tags g ON g.id = tt.tag_id"
                " WHERE tt.transaction_id = ?",
                (transaction_id,),
            )
        )
        links = frozenset(
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM links WHERE transaction_id = ?",
                (transaction_id,),
            )
        )
        return self._row_to_transaction(row, postings, tags, links)

    def get_transaction_by_source(
        self, source: str | None, source_ref: str
    ) -> Transaction | None:
        row = self._conn.execute(
            "SELECT id FROM transactions WHERE source IS ? AND source_ref = ?",
            (source, source_ref),
        ).fetchone()
        return self.get_transaction(int(row[0])) if row else None

    def list_transactions(
        self, *, tag: str | None = None, link: str | None = None
    ) -> list[Transaction]:
        """Every transaction with its postings — optionally only those
        carrying `tag` and/or `link` — ordered by (date, id), the
        deterministic order export relies on."""
        query = "SELECT t.* FROM transactions t"
        params: list[object] = []
        if tag is not None:
            query += (
                " JOIN transaction_tags tt ON tt.transaction_id = t.id"
                " JOIN tags g ON g.id = tt.tag_id AND g.name = ?"
            )
            params.append(tag)
        if link is not None:
            query += " JOIN links l ON l.transaction_id = t.id AND l.name = ?"
            params.append(link)
        txn_rows = (
            self._cursor().execute(query + " ORDER BY t.date, t.id", params).fetchall()
        )
        posting_rows = (
            self._cursor()
            .execute(
                "SELECT p.*, a.path AS account_path"
                " FROM postings p"
                " JOIN accounts a ON a.id = p.account_id"
                " ORDER BY p.transaction_id, p.seq"
            )
            .fetchall()
        )
        commodities = self._commodity_map()
        matches = self._matches_by_posting()
        postings_by_txn: dict[int, list[Posting]] = {}
        for row in posting_rows:
            postings_by_txn.setdefault(row["transaction_id"], []).append(
                self._row_to_posting(row, commodities, matches)
            )
        tags_by_txn: dict[int, set[str]] = {}
        for txn_id, name in self._conn.execute(
            "SELECT tt.transaction_id, g.name FROM transaction_tags tt"
            " JOIN tags g ON g.id = tt.tag_id"
        ):
            tags_by_txn.setdefault(txn_id, set()).add(name)
        links_by_txn: dict[int, set[str]] = {}
        for txn_id, name in self._conn.execute(
            "SELECT transaction_id, name FROM links"
        ):
            links_by_txn.setdefault(txn_id, set()).add(name)
        return [
            self._row_to_transaction(
                row,
                tuple(postings_by_txn.get(row["id"], ())),
                frozenset(tags_by_txn.get(row["id"], ())),
                frozenset(links_by_txn.get(row["id"], ())),
            )
            for row in txn_rows
        ]

    @staticmethod
    def _row_to_transaction(
        row: sqlite3.Row,
        postings: tuple[Posting, ...],
        tags: frozenset[str],
        links: frozenset[str],
    ) -> Transaction:
        return Transaction(
            id=row["id"],
            date=datetime.date.fromisoformat(row["date"]),
            flag=row["flag"],
            payee=row["payee"],
            narration=row["narration"],
            tags=tags,
            links=links,
            source=row["source"],
            source_ref=row["source_ref"],
            generated=bool(row["generated"]),
            reverses_id=row["reverses_id"],
            created_at=datetime.datetime.fromisoformat(row["created_at"]),
            postings=postings,
            metadata=_load_metadata(row["metadata"]),
        )

    @staticmethod
    def _row_to_posting(
        row: sqlite3.Row,
        commodities: dict[int, Commodity],
        matches: dict[int, tuple[LotMatch, ...]],
    ) -> Posting:
        commodity = commodities[row["commodity_id"]]
        units = Amount(
            value=row["units"],
            precision=row["units_precision"],
            commodity=commodity,
        )
        lot_matches = matches.get(row["id"], ())

        cost: Cost | None = None
        if row["cost_per_unit"] is not None:
            cost = Cost(
                per_unit=Amount(
                    value=row["cost_per_unit"],
                    precision=row["cost_precision"],
                    commodity=commodities[row["cost_commodity"]],
                ),
                date=datetime.date.fromisoformat(row["cost_date"])
                if row["cost_date"]
                else None,
                label=row["cost_label"],
            )
        elif (
            row["cost_commodity"] is not None
            or row["cost_date"] is not None
            or row["cost_label"] is not None
        ):
            cost = Cost(
                commodity=commodities[row["cost_commodity"]].symbol
                if row["cost_commodity"] is not None
                else None,
                date=datetime.date.fromisoformat(row["cost_date"])
                if row["cost_date"]
                else None,
                label=row["cost_label"],
            )
        elif lot_matches:
            # An empty {} filter stores all-NULL cost columns; the posting
            # having lot_reductions is what marks it as cost-carrying.
            cost = Cost()

        price: Amount | None = None
        if row["price_per_unit"] is not None:
            price = Amount(
                value=row["price_per_unit"],
                precision=row["price_precision"],
                commodity=commodities[row["price_commodity"]],
            )

        # The schema has no separate weight precision: a weight equal to
        # its units reuses the written precision, a computed one (cost or
        # price present) is a full scale-8 product (design §5).
        weight_commodity = commodities[row["weight_commodity"]]
        weight = Amount(
            value=row["weight"],
            precision=row["units_precision"]
            if weight_commodity == commodity and cost is None and price is None
            else SCALE,
            commodity=weight_commodity,
        )
        return Posting(
            account=row["account_path"],
            units=units,
            weight=weight,
            cost=cost,
            price=price,
            lot_matches=lot_matches,
            flag=row["flag"],
            interpolated=bool(row["interpolated"]),
            metadata=_load_metadata(row["metadata"]),
        )

    # --- balance assertions ------------------------------------------------

    def add_assertion(self, assertion: BalanceAssertion) -> BalanceAssertion:
        with unit_of_work(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO balance_assertions"
                " (date, account_id, amount, precision, commodity_id,"
                "  source, checked_at, status, difference)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assertion.date.isoformat(),
                    self._account_id(assertion.account),
                    assertion.amount.value,
                    assertion.amount.precision,
                    self._commodity_id(assertion.amount.commodity.symbol),
                    assertion.source,
                    assertion.checked_at.isoformat() if assertion.checked_at else None,
                    assertion.status.value,
                    assertion.difference.value if assertion.difference else None,
                ),
            )
        assert cursor.lastrowid is not None
        return dataclasses.replace(assertion, id=cursor.lastrowid)

    @staticmethod
    def _row_to_assertion(row: sqlite3.Row) -> BalanceAssertion:
        commodity = Commodity(
            symbol=row["symbol"],
            kind=CommodityKind(row["kind"]),
            display_precision=row["display_precision"],
        )
        amount = Amount(
            value=row["amount"], precision=row["precision"], commodity=commodity
        )
        difference = (
            Amount(
                value=row["difference"],
                precision=row["precision"],
                commodity=commodity,
            )
            if row["difference"] is not None
            else None
        )
        return BalanceAssertion(
            id=row["id"],
            date=datetime.date.fromisoformat(row["date"]),
            account=row["account_path"],
            amount=amount,
            source=row["source"],
            status=AssertionStatus(row["status"]),
            difference=difference,
            checked_at=datetime.datetime.fromisoformat(row["checked_at"])
            if row["checked_at"]
            else None,
        )

    _ASSERTION_SELECT = (
        "SELECT b.*, a.path AS account_path, c.symbol, c.kind,"
        "       c.display_precision"
        " FROM balance_assertions b"
        " JOIN accounts a ON a.id = b.account_id"
        " JOIN commodities c ON c.id = b.commodity_id"
    )

    def get_assertion(self, assertion_id: int) -> BalanceAssertion | None:
        row = (
            self._cursor()
            .execute(self._ASSERTION_SELECT + " WHERE b.id = ?", (assertion_id,))
            .fetchone()
        )
        return self._row_to_assertion(row) if row else None

    def list_assertions(self, account: str | None = None) -> list[BalanceAssertion]:
        query, params = self._ASSERTION_SELECT, ()
        if account is not None:
            query += " WHERE a.path = ?"
            params = (account,)
        rows = (
            self._cursor().execute(query + " ORDER BY b.date, b.id", params).fetchall()
        )
        return [self._row_to_assertion(row) for row in rows]

    def update_assertion_check(
        self,
        assertion_id: int,
        status: AssertionStatus,
        difference: int,
        checked_at: datetime.datetime,
    ) -> None:
        with unit_of_work(self._conn):
            self._conn.execute(
                "UPDATE balance_assertions"
                " SET status = ?, difference = ?, checked_at = ?"
                " WHERE id = ?",
                (status.value, difference, checked_at.isoformat(), assertion_id),
            )

    # --- pads --------------------------------------------------------------

    def add_pad(self, pad: Pad) -> Pad:
        with unit_of_work(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO pads"
                " (date, account_id, source_account, consumed_by, generated_txn)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    pad.date.isoformat(),
                    self._account_id(pad.account),
                    self._account_id(pad.source_account),
                    pad.consumed_by,
                    pad.generated_txn_id,
                ),
            )
        assert cursor.lastrowid is not None
        return dataclasses.replace(pad, id=cursor.lastrowid)

    @staticmethod
    def _row_to_pad(row: sqlite3.Row) -> Pad:
        return Pad(
            id=row["id"],
            date=datetime.date.fromisoformat(row["date"]),
            account=row["account_path"],
            source_account=row["source_path"],
            consumed_by=row["consumed_by"],
            generated_txn_id=row["generated_txn"],
        )

    _PAD_SELECT = (
        "SELECT p.*, a.path AS account_path, s.path AS source_path"
        " FROM pads p"
        " JOIN accounts a ON a.id = p.account_id"
        " JOIN accounts s ON s.id = p.source_account"
    )

    def get_pad(self, pad_id: int) -> Pad | None:
        row = (
            self._cursor()
            .execute(self._PAD_SELECT + " WHERE p.id = ?", (pad_id,))
            .fetchone()
        )
        return self._row_to_pad(row) if row else None

    def list_pads(self, account: str | None = None) -> list[Pad]:
        query, params = self._PAD_SELECT, ()
        if account is not None:
            query += " WHERE a.path = ?"
            params = (account,)
        rows = (
            self._cursor().execute(query + " ORDER BY p.date, p.id", params).fetchall()
        )
        return [self._row_to_pad(row) for row in rows]

    def consume_pad(
        self,
        pad_id: int,
        assertion_id: int,
        transaction_id: int | None,
    ) -> None:
        with unit_of_work(self._conn):
            self._conn.execute(
                "UPDATE pads SET consumed_by = ?, generated_txn = ? WHERE id = ?",
                (assertion_id, transaction_id, pad_id),
            )

    # --- hub attachments ---------------------------------------------------

    def add_note(self, note: Note) -> Note:
        with unit_of_work(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO notes (date, account_id, comment) VALUES (?, ?, ?)",
                (
                    note.date.isoformat(),
                    self._account_id(note.account),
                    note.comment,
                ),
            )
        assert cursor.lastrowid is not None
        return dataclasses.replace(note, id=cursor.lastrowid)

    def list_notes(self, account: str | None = None) -> list[Note]:
        query = (
            "SELECT n.*, a.path AS account_path FROM notes n"
            " JOIN accounts a ON a.id = n.account_id"
        )
        params: tuple[object, ...] = ()
        if account is not None:
            query += " WHERE a.path = ?"
            params = (account,)
        rows = (
            self._cursor().execute(query + " ORDER BY n.date, n.id", params).fetchall()
        )
        return [
            Note(
                id=row["id"],
                date=datetime.date.fromisoformat(row["date"]),
                account=row["account_path"],
                comment=row["comment"],
            )
            for row in rows
        ]

    def add_document(self, document: Document) -> Document:
        with unit_of_work(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO documents (date, account_id, path, sha256)"
                " VALUES (?, ?, ?, ?)",
                (
                    document.date.isoformat(),
                    self._account_id(document.account),
                    document.path,
                    document.sha256,
                ),
            )
        assert cursor.lastrowid is not None
        return dataclasses.replace(document, id=cursor.lastrowid)

    def list_documents(self, account: str | None = None) -> list[Document]:
        query = (
            "SELECT d.*, a.path AS account_path FROM documents d"
            " JOIN accounts a ON a.id = d.account_id"
        )
        params: tuple[object, ...] = ()
        if account is not None:
            query += " WHERE a.path = ?"
            params = (account,)
        rows = (
            self._cursor().execute(query + " ORDER BY d.date, d.id", params).fetchall()
        )
        return [
            Document(
                id=row["id"],
                date=datetime.date.fromisoformat(row["date"]),
                account=row["account_path"],
                path=row["path"],
                sha256=row["sha256"],
            )
            for row in rows
        ]

    def add_event(self, event: Event) -> Event:
        with unit_of_work(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO events (date, type, value) VALUES (?, ?, ?)",
                (event.date.isoformat(), event.type, event.value),
            )
        assert cursor.lastrowid is not None
        return dataclasses.replace(event, id=cursor.lastrowid)

    def list_events(self, type: str | None = None) -> list[Event]:
        query, params = "SELECT * FROM events", ()
        if type is not None:
            query += " WHERE type = ?"
            params = (type,)
        rows = self._cursor().execute(query + " ORDER BY date, id", params).fetchall()
        return [
            Event(
                id=row["id"],
                date=datetime.date.fromisoformat(row["date"]),
                type=row["type"],
                value=row["value"],
            )
            for row in rows
        ]

    # --- lots (M7) ---------------------------------------------------------

    _LOT_SELECT = (
        "SELECT l.*, a.path AS account_path, t.date AS recorded_on,"
        "       p.transaction_id AS opened_txn, p.seq AS opened_seq"
        " FROM lots l"
        " JOIN accounts a ON a.id = l.account_id"
        " JOIN postings p ON p.id = l.opened_by_posting"
        " JOIN transactions t ON t.id = p.transaction_id"
    )

    @staticmethod
    def _row_to_lot(row: sqlite3.Row, commodities: dict[int, Commodity]) -> Lot:
        return Lot(
            id=row["id"],
            account=row["account_path"],
            commodity=commodities[row["commodity_id"]],
            acquired_on=datetime.date.fromisoformat(row["acquired_on"]),
            original_quantity=row["original_quantity"],
            cost=Amount(
                value=row["cost_per_unit"],
                precision=row["cost_precision"],
                commodity=commodities[row["cost_commodity"]],
            ),
            label=row["label"],
            recorded_on=datetime.date.fromisoformat(row["recorded_on"]),
            opened_by_transaction_id=row["opened_txn"],
            opened_by_seq=row["opened_seq"],
        )

    def list_lots(
        self,
        account: str | None = None,
        symbol: str | None = None,
        *,
        include_children: bool = False,
    ) -> list[Lot]:
        where: list[str] = []
        params: list[object] = []
        if account is not None:
            if include_children:
                where.append("(a.path = ? OR a.path LIKE ?)")
                params.extend([account, account + ":%"])
            else:
                where.append("a.path = ?")
                params.append(account)
        if symbol is not None:
            where.append(
                "l.commodity_id = (SELECT id FROM commodities WHERE symbol = ?)"
            )
            params.append(symbol)
        query = self._LOT_SELECT
        if where:
            query += " WHERE " + " AND ".join(where)
        rows = self._cursor().execute(query + " ORDER BY l.id", params).fetchall()
        commodities = self._commodity_map()
        return [self._row_to_lot(row, commodities) for row in rows]

    def get_lot_by_opening(self, transaction_id: int, seq: int) -> Lot | None:
        row = (
            self._cursor()
            .execute(
                self._LOT_SELECT + " WHERE p.transaction_id = ? AND p.seq = ?",
                (transaction_id, seq),
            )
            .fetchone()
        )
        return self._row_to_lot(row, self._commodity_map()) if row else None

    def list_lot_reductions(
        self, lot_ids: Collection[int] | None = None
    ) -> list[LotReductionEntry]:
        query = (
            "SELECT r.*, p.transaction_id, p.seq, t.date"
            " FROM lot_reductions r"
            " JOIN postings p ON p.id = r.posting_id"
            " JOIN transactions t ON t.id = p.transaction_id"
        )
        params: list[object] = []
        if lot_ids is not None:
            if not lot_ids:
                return []
            placeholders = ", ".join("?" for _ in lot_ids)
            query += f" WHERE r.lot_id IN ({placeholders})"
            params.extend(lot_ids)
        rows = self._cursor().execute(query + " ORDER BY r.id", params).fetchall()
        return [
            LotReductionEntry(
                id=row["id"],
                lot_id=row["lot_id"],
                transaction_id=row["transaction_id"],
                seq=row["seq"],
                quantity=row["quantity"],
                date=datetime.date.fromisoformat(row["date"]),
            )
            for row in rows
        ]

    # --- prices (M7) -------------------------------------------------------

    def add_price(self, price: PricePoint, *, replace: bool = False) -> PricePoint:
        commodity_id = self._commodity_id(price.commodity)
        quote_id = self._commodity_id(price.price.commodity.symbol)
        with unit_of_work(self._conn):
            row = self._conn.execute(
                "SELECT id FROM prices"
                " WHERE date = ? AND commodity_id = ? AND quote_commodity = ?",
                (price.date.isoformat(), commodity_id, quote_id),
            ).fetchone()
            if row is not None:
                if not replace:
                    existing = self._get_price(int(row[0]))
                    assert existing is not None
                    return existing
                self._conn.execute(
                    "UPDATE prices"
                    " SET price = ?, price_precision = ?, origin = ?"
                    " WHERE id = ?",
                    (
                        price.price.value,
                        price.price.precision,
                        price.origin,
                        row[0],
                    ),
                )
                return dataclasses.replace(price, id=int(row[0]))
            cursor = self._conn.execute(
                "INSERT INTO prices"
                " (date, commodity_id, price, price_precision,"
                "  quote_commodity, origin)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    price.date.isoformat(),
                    commodity_id,
                    price.price.value,
                    price.price.precision,
                    quote_id,
                    price.origin,
                ),
            )
        assert cursor.lastrowid is not None
        return dataclasses.replace(price, id=cursor.lastrowid)

    @staticmethod
    def _row_to_price(
        row: sqlite3.Row, commodities: dict[int, Commodity]
    ) -> PricePoint:
        return PricePoint(
            id=row["id"],
            date=datetime.date.fromisoformat(row["date"]),
            commodity=commodities[row["commodity_id"]].symbol,
            price=Amount(
                value=row["price"],
                precision=row["price_precision"],
                commodity=commodities[row["quote_commodity"]],
            ),
            origin=row["origin"],
        )

    def _get_price(self, price_id: int) -> PricePoint | None:
        row = (
            self._cursor()
            .execute("SELECT * FROM prices WHERE id = ?", (price_id,))
            .fetchone()
        )
        return self._row_to_price(row, self._commodity_map()) if row else None

    def list_prices(
        self, symbol: str | None = None, quote: str | None = None
    ) -> list[PricePoint]:
        where: list[str] = []
        params: list[object] = []
        if symbol is not None:
            where.append("commodity_id = (SELECT id FROM commodities WHERE symbol = ?)")
            params.append(symbol)
        if quote is not None:
            where.append(
                "quote_commodity = (SELECT id FROM commodities WHERE symbol = ?)"
            )
            params.append(quote)
        query = "SELECT * FROM prices"
        if where:
            query += " WHERE " + " AND ".join(where)
        rows = self._cursor().execute(query + " ORDER BY date, id", params).fetchall()
        commodities = self._commodity_map()
        return [self._row_to_price(row, commodities) for row in rows]

    def latest_price(
        self, symbol: str, quote: str, on: datetime.date | None = None
    ) -> PricePoint | None:
        query = (
            "SELECT * FROM prices"
            " WHERE commodity_id = (SELECT id FROM commodities WHERE symbol = ?)"
            " AND quote_commodity = (SELECT id FROM commodities WHERE symbol = ?)"
        )
        params: list[object] = [symbol, quote]
        if on is not None:
            query += " AND date <= ?"
            params.append(on.isoformat())
        row = (
            self._cursor()
            .execute(query + " ORDER BY date DESC, id DESC LIMIT 1", params)
            .fetchone()
        )
        return self._row_to_price(row, self._commodity_map()) if row else None

    # --- storage integrity -------------------------------------------------

    def storage_integrity(self) -> list[str]:
        findings = []
        for row in self._conn.execute("PRAGMA foreign_key_check"):
            findings.append(
                f"dangling foreign key: {row[0]} rowid {row[1]} -> {row[2]}"
            )
        rows = self._conn.execute(
            "SELECT child.path, parent.path"
            " FROM accounts child JOIN accounts parent"
            "   ON parent.id = child.parent_id"
        ).fetchall()
        for child_path, actual_parent in rows:
            expected = parent_path(child_path)
            if actual_parent != expected:
                findings.append(
                    f"account {child_path!r} links parent {actual_parent!r},"
                    f" expected {expected!r}"
                )
        rows = self._conn.execute(
            "SELECT source, source_ref, COUNT(*) FROM transactions"
            " WHERE source_ref IS NOT NULL"
            " GROUP BY source, source_ref HAVING COUNT(*) > 1"
        ).fetchall()
        for source, source_ref, count in rows:
            findings.append(
                f"{count} transactions share source={source!r},"
                f" source_ref={source_ref!r}"
            )
        return findings

    # --- queries -----------------------------------------------------------

    def journal(
        self,
        account: str,
        *,
        include_children: bool = True,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
        tag: str | None = None,
        link: str | None = None,
    ) -> list[JournalEntry]:
        where = ["a.path = ?"]
        params: list[object] = [account]
        if include_children:
            where[0] = "(a.path = ? OR a.path LIKE ?)"
            params.append(account + ":%")
        if start is not None:
            where.append("t.date >= ?")
            params.append(start.isoformat())
        if end is not None:
            where.append("t.date <= ?")
            params.append(end.isoformat())
        if tag is not None:
            where.append(
                "EXISTS (SELECT 1 FROM transaction_tags tt"
                " JOIN tags g ON g.id = tt.tag_id"
                " WHERE tt.transaction_id = t.id AND g.name = ?)"
            )
            params.append(tag)
        if link is not None:
            where.append(
                "EXISTS (SELECT 1 FROM links l"
                " WHERE l.transaction_id = t.id AND l.name = ?)"
            )
            params.append(link)
        rows = (
            self._cursor()
            .execute(
                "SELECT p.*, a.path AS account_path,"
                "       t.id AS txn_id, t.date AS txn_date, t.flag AS txn_flag,"
                "       t.payee, t.narration"
                " FROM postings p"
                " JOIN transactions t ON t.id = p.transaction_id"
                " JOIN accounts a ON a.id = p.account_id"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY t.date, t.id, p.seq",
                params,
            )
            .fetchall()
        )
        commodities = self._commodity_map()
        matches = self._matches_by_posting()
        return [
            JournalEntry(
                transaction_id=row["txn_id"],
                date=datetime.date.fromisoformat(row["txn_date"]),
                flag=row["txn_flag"],
                payee=row["payee"],
                narration=row["narration"],
                posting=self._row_to_posting(row, commodities, matches),
            )
            for row in rows
        ]

    def balance(
        self,
        account: str,
        *,
        include_children: bool = True,
        on: datetime.date | None = None,
    ) -> dict[str, int]:
        where = ["a.path = ?"]
        params: list[object] = [account]
        if include_children:
            where[0] = "(a.path = ? OR a.path LIKE ?)"
            params.append(account + ":%")
        if on is not None:
            where.append("t.date <= ?")
            params.append(on.isoformat())
        rows = self._conn.execute(
            "SELECT c.symbol, SUM(p.units) AS total"
            " FROM postings p"
            " JOIN transactions t ON t.id = p.transaction_id"
            " JOIN accounts a ON a.id = p.account_id"
            " JOIN commodities c ON c.id = p.commodity_id"
            f" WHERE {' AND '.join(where)}"
            " GROUP BY c.symbol"
            " HAVING SUM(p.units) != 0",
            params,
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def balances_by_account(
        self,
        *,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
    ) -> dict[str, dict[str, int]]:
        where: list[str] = []
        params: list[object] = []
        if start is not None:
            where.append("t.date >= ?")
            params.append(start.isoformat())
        if end is not None:
            where.append("t.date <= ?")
            params.append(end.isoformat())
        rows = self._conn.execute(
            "SELECT a.path, c.symbol, SUM(p.units) AS total"
            " FROM postings p"
            " JOIN transactions t ON t.id = p.transaction_id"
            " JOIN accounts a ON a.id = p.account_id"
            " JOIN commodities c ON c.id = p.commodity_id"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + " GROUP BY a.path, c.symbol"
            " HAVING SUM(p.units) != 0",
            params,
        ).fetchall()
        totals: dict[str, dict[str, int]] = {}
        for row in rows:
            totals.setdefault(row[0], {})[row[1]] = int(row[2])
        return totals


class InMemoryRepository:
    """Reference backend: plain dicts, no SQL, same contract.

    Metadata goes through a JSON round-trip so both backends accept and
    normalize exactly the same values.
    """

    def __init__(self) -> None:
        self._commodities: dict[str, Commodity] = {}
        self._accounts: dict[str, Account] = {}
        self._options: dict[str, str] = {}
        self._transactions: dict[int, Transaction] = {}
        self._source_refs: dict[tuple[str | None, str], int] = {}
        self._assertions: dict[int, BalanceAssertion] = {}
        self._pads: dict[int, Pad] = {}
        self._notes: dict[int, Note] = {}
        self._documents: dict[int, Document] = {}
        self._events: dict[int, Event] = {}
        self._lots: dict[int, Lot] = {}
        self._prices: dict[int, PricePoint] = {}
        self._price_index: dict[tuple[datetime.date, str, str], int] = {}
        self._next_id = 1
        self._next_assertion_id = 1
        self._next_pad_id = 1
        self._next_note_id = 1
        self._next_document_id = 1
        self._next_event_id = 1
        self._next_lot_id = 1
        self._next_price_id = 1

    # --- commodities -------------------------------------------------------

    def add_commodity(
        self,
        commodity: Commodity,
        *,
        name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        del name, metadata  # stored only for product display; not read in M1
        if commodity.symbol in self._commodities:
            raise DuplicateCommodityError(
                f"commodity {commodity.symbol!r} already exists"
            )
        self._commodities[commodity.symbol] = commodity

    def get_commodity(self, symbol: str) -> Commodity | None:
        return self._commodities.get(symbol)

    def list_commodities(self) -> list[Commodity]:
        return [self._commodities[s] for s in sorted(self._commodities)]

    # --- accounts ----------------------------------------------------------

    def add_account(self, account: Account) -> None:
        if account.path in self._accounts:
            raise DuplicateAccountError(f"account {account.path!r} already exists")
        self._accounts[account.path] = dataclasses.replace(
            account, metadata=_normalize_metadata(account.metadata)
        )

    def get_account(self, path: str) -> Account | None:
        return self._accounts.get(path)

    def list_accounts(self) -> list[Account]:
        return [self._accounts[p] for p in sorted(self._accounts)]

    def close_account(self, path: str, closed_on: datetime.date) -> None:
        account = self._accounts.get(path)
        if account is None:
            raise UnknownAccountError(path)
        if account.closed_on is not None:
            raise AccountError(
                f"account {path!r} is already closed on {account.closed_on}"
            )
        if closed_on < account.opened_on:
            raise AccountError(
                f"cannot close {path!r} on {closed_on}, "
                f"before it opened on {account.opened_on}"
            )
        self._accounts[path] = dataclasses.replace(account, closed_on=closed_on)

    # --- options -----------------------------------------------------------

    def set_option(self, key: str, value: str) -> None:
        self._options[key] = value

    def get_option(self, key: str) -> str | None:
        return self._options.get(key)

    def list_options(self) -> dict[str, str]:
        return dict(self._options)

    # --- transactions ------------------------------------------------------

    def add_transaction(self, transaction: Transaction) -> Transaction:
        if transaction.source_ref is not None:
            key = (transaction.source, transaction.source_ref)
            if key in self._source_refs:
                raise DuplicateSourceError(
                    f"transaction with source={transaction.source!r},"
                    f" source_ref={transaction.source_ref!r} already exists"
                    f" (id {self._source_refs[key]})"
                )
        for posting in transaction.postings:
            if posting.account not in self._accounts:
                raise UnknownAccountError(posting.account)
            if posting.units.commodity.symbol not in self._commodities:
                raise UnknownCommodityError(posting.units.commodity.symbol)
            cost_symbol = (
                posting.cost.cost_commodity if posting.cost is not None else None
            )
            if cost_symbol is not None and cost_symbol not in self._commodities:
                raise UnknownCommodityError(cost_symbol)
            if (
                posting.price is not None
                and posting.price.commodity.symbol not in self._commodities
            ):
                raise UnknownCommodityError(posting.price.commodity.symbol)

        transaction_id = self._next_id
        self._next_id += 1
        stored = dataclasses.replace(
            transaction,
            id=transaction_id,
            created_at=transaction.created_at or datetime.datetime.now(datetime.UTC),
            tags=frozenset(transaction.tags),
            links=frozenset(transaction.links),
            metadata=_normalize_metadata(transaction.metadata),
            postings=tuple(
                dataclasses.replace(
                    posting, metadata=_normalize_metadata(posting.metadata)
                )
                for posting in transaction.postings
            ),
        )
        self._transactions[transaction_id] = stored
        if transaction.source_ref is not None:
            self._source_refs[(transaction.source, transaction.source_ref)] = (
                transaction_id
            )
        for seq, posting in enumerate(stored.postings):
            cost = posting.cost
            if cost is not None and posting.units.value > 0 and not posting.lot_matches:
                assert cost.per_unit is not None and cost.date is not None
                lot_id = self._next_lot_id
                self._next_lot_id += 1
                self._lots[lot_id] = Lot(
                    id=lot_id,
                    account=posting.account,
                    commodity=posting.units.commodity,
                    acquired_on=cost.date,
                    original_quantity=posting.units.value,
                    cost=cost.per_unit,
                    label=cost.label,
                    recorded_on=stored.date,
                    opened_by_transaction_id=transaction_id,
                    opened_by_seq=seq,
                )
        return stored

    def get_transaction(self, transaction_id: int) -> Transaction | None:
        return self._transactions.get(transaction_id)

    def get_transaction_by_source(
        self, source: str | None, source_ref: str
    ) -> Transaction | None:
        transaction_id = self._source_refs.get((source, source_ref))
        if transaction_id is None:
            return None
        return self._transactions[transaction_id]

    def list_transactions(
        self, *, tag: str | None = None, link: str | None = None
    ) -> list[Transaction]:
        transactions = [
            t
            for t in self._transactions.values()
            if (tag is None or tag in t.tags) and (link is None or link in t.links)
        ]
        return sorted(transactions, key=lambda t: (t.date, t.id))

    # --- hub attachments ---------------------------------------------------

    def add_note(self, note: Note) -> Note:
        if note.account not in self._accounts:
            raise UnknownAccountError(note.account)
        note_id = self._next_note_id
        self._next_note_id += 1
        stored = dataclasses.replace(note, id=note_id)
        self._notes[note_id] = stored
        return stored

    def list_notes(self, account: str | None = None) -> list[Note]:
        notes = [
            n for n in self._notes.values() if account is None or n.account == account
        ]
        return sorted(notes, key=lambda n: (n.date, n.id))

    def add_document(self, document: Document) -> Document:
        if document.account not in self._accounts:
            raise UnknownAccountError(document.account)
        document_id = self._next_document_id
        self._next_document_id += 1
        stored = dataclasses.replace(document, id=document_id)
        self._documents[document_id] = stored
        return stored

    def list_documents(self, account: str | None = None) -> list[Document]:
        documents = [
            d
            for d in self._documents.values()
            if account is None or d.account == account
        ]
        return sorted(documents, key=lambda d: (d.date, d.id))

    def add_event(self, event: Event) -> Event:
        event_id = self._next_event_id
        self._next_event_id += 1
        stored = dataclasses.replace(event, id=event_id)
        self._events[event_id] = stored
        return stored

    def list_events(self, type: str | None = None) -> list[Event]:
        events = [e for e in self._events.values() if type is None or e.type == type]
        return sorted(events, key=lambda e: (e.date, e.id))

    # --- balance assertions ------------------------------------------------

    def add_assertion(self, assertion: BalanceAssertion) -> BalanceAssertion:
        if assertion.account not in self._accounts:
            raise UnknownAccountError(assertion.account)
        if assertion.amount.commodity.symbol not in self._commodities:
            raise UnknownCommodityError(assertion.amount.commodity.symbol)
        assertion_id = self._next_assertion_id
        self._next_assertion_id += 1
        stored = dataclasses.replace(assertion, id=assertion_id)
        self._assertions[assertion_id] = stored
        return stored

    def get_assertion(self, assertion_id: int) -> BalanceAssertion | None:
        return self._assertions.get(assertion_id)

    def list_assertions(self, account: str | None = None) -> list[BalanceAssertion]:
        assertions = [
            a
            for a in self._assertions.values()
            if account is None or a.account == account
        ]
        return sorted(assertions, key=lambda a: (a.date, a.id))

    def update_assertion_check(
        self,
        assertion_id: int,
        status: AssertionStatus,
        difference: int,
        checked_at: datetime.datetime,
    ) -> None:
        assertion = self._assertions[assertion_id]
        self._assertions[assertion_id] = dataclasses.replace(
            assertion,
            status=status,
            difference=Amount(
                value=difference,
                precision=assertion.amount.precision,
                commodity=assertion.amount.commodity,
            ),
            checked_at=checked_at,
        )

    # --- pads --------------------------------------------------------------

    def add_pad(self, pad: Pad) -> Pad:
        for path in (pad.account, pad.source_account):
            if path not in self._accounts:
                raise UnknownAccountError(path)
        pad_id = self._next_pad_id
        self._next_pad_id += 1
        stored = dataclasses.replace(pad, id=pad_id)
        self._pads[pad_id] = stored
        return stored

    def get_pad(self, pad_id: int) -> Pad | None:
        return self._pads.get(pad_id)

    def list_pads(self, account: str | None = None) -> list[Pad]:
        pads = [
            p for p in self._pads.values() if account is None or p.account == account
        ]
        return sorted(pads, key=lambda p: (p.date, p.id))

    def consume_pad(
        self,
        pad_id: int,
        assertion_id: int,
        transaction_id: int | None,
    ) -> None:
        self._pads[pad_id] = dataclasses.replace(
            self._pads[pad_id],
            consumed_by=assertion_id,
            generated_txn_id=transaction_id,
        )

    # --- lots (M7) ---------------------------------------------------------

    def list_lots(
        self,
        account: str | None = None,
        symbol: str | None = None,
        *,
        include_children: bool = False,
    ) -> list[Lot]:
        lots = [
            lot
            for lot in self._lots.values()
            if (
                account is None
                or _account_matches(lot.account, account, include_children)
            )
            and (symbol is None or lot.commodity.symbol == symbol)
        ]
        return sorted(lots, key=lambda lot: lot.id or 0)

    def get_lot_by_opening(self, transaction_id: int, seq: int) -> Lot | None:
        for lot in self._lots.values():
            if (
                lot.opened_by_transaction_id == transaction_id
                and lot.opened_by_seq == seq
            ):
                return lot
        return None

    def list_lot_reductions(
        self, lot_ids: Collection[int] | None = None
    ) -> list[LotReductionEntry]:
        # Derived from the stored postings' lot matches, in entry order —
        # matches are the reduction rows, so the two views cannot drift.
        entries: list[LotReductionEntry] = []
        next_id = 1
        for transaction_id in sorted(self._transactions):
            transaction = self._transactions[transaction_id]
            for seq, posting in enumerate(transaction.postings):
                for match in posting.lot_matches:
                    entry_id = next_id
                    next_id += 1
                    if lot_ids is not None and match.lot_id not in lot_ids:
                        continue
                    entries.append(
                        LotReductionEntry(
                            id=entry_id,
                            lot_id=match.lot_id,
                            transaction_id=transaction_id,
                            seq=seq,
                            quantity=match.quantity,
                            date=transaction.date,
                        )
                    )
        return entries

    # --- prices (M7) -------------------------------------------------------

    def add_price(self, price: PricePoint, *, replace: bool = False) -> PricePoint:
        for symbol in (price.commodity, price.price.commodity.symbol):
            if symbol not in self._commodities:
                raise UnknownCommodityError(symbol)
        key = (price.date, price.commodity, price.price.commodity.symbol)
        existing_id = self._price_index.get(key)
        if existing_id is not None:
            if not replace:
                return self._prices[existing_id]
            stored = dataclasses.replace(price, id=existing_id)
            self._prices[existing_id] = stored
            return stored
        price_id = self._next_price_id
        self._next_price_id += 1
        stored = dataclasses.replace(price, id=price_id)
        self._prices[price_id] = stored
        self._price_index[key] = price_id
        return stored

    def list_prices(
        self, symbol: str | None = None, quote: str | None = None
    ) -> list[PricePoint]:
        prices = [
            price
            for price in self._prices.values()
            if (symbol is None or price.commodity == symbol)
            and (quote is None or price.price.commodity.symbol == quote)
        ]
        return sorted(prices, key=lambda price: (price.date, price.id or 0))

    def latest_price(
        self, symbol: str, quote: str, on: datetime.date | None = None
    ) -> PricePoint | None:
        best: PricePoint | None = None
        for price in self._prices.values():
            if price.commodity != symbol or price.price.commodity.symbol != quote:
                continue
            if on is not None and price.date > on:
                continue
            if best is None or (price.date, price.id or 0) > (best.date, best.id or 0):
                best = price
        return best

    # --- storage integrity -------------------------------------------------

    def storage_integrity(self) -> list[str]:
        # Plain dicts holding whole domain objects cannot express dangling
        # references or orphan rows; nothing to check.
        return []

    # --- queries -----------------------------------------------------------

    def journal(
        self,
        account: str,
        *,
        include_children: bool = True,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
        tag: str | None = None,
        link: str | None = None,
    ) -> list[JournalEntry]:
        entries: list[JournalEntry] = []
        for transaction_id in sorted(
            self._transactions,
            key=lambda i: (self._transactions[i].date, i),
        ):
            transaction = self._transactions[transaction_id]
            if start is not None and transaction.date < start:
                continue
            if end is not None and transaction.date > end:
                continue
            if tag is not None and tag not in transaction.tags:
                continue
            if link is not None and link not in transaction.links:
                continue
            for posting in transaction.postings:
                if _account_matches(posting.account, account, include_children):
                    entries.append(
                        JournalEntry(
                            transaction_id=transaction_id,
                            date=transaction.date,
                            flag=transaction.flag,
                            payee=transaction.payee,
                            narration=transaction.narration,
                            posting=posting,
                        )
                    )
        return entries

    def balance(
        self,
        account: str,
        *,
        include_children: bool = True,
        on: datetime.date | None = None,
    ) -> dict[str, int]:
        totals: dict[str, int] = {}
        for transaction in self._transactions.values():
            if on is not None and transaction.date > on:
                continue
            for posting in transaction.postings:
                if _account_matches(posting.account, account, include_children):
                    symbol = posting.units.commodity.symbol
                    totals[symbol] = totals.get(symbol, 0) + posting.units.value
        return {symbol: total for symbol, total in totals.items() if total}

    def balances_by_account(
        self,
        *,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
    ) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        for transaction in self._transactions.values():
            if start is not None and transaction.date < start:
                continue
            if end is not None and transaction.date > end:
                continue
            for posting in transaction.postings:
                by_symbol = totals.setdefault(posting.account, {})
                symbol = posting.units.commodity.symbol
                by_symbol[symbol] = by_symbol.get(symbol, 0) + posting.units.value
        return {
            path: {symbol: value for symbol, value in by_symbol.items() if value}
            for path, by_symbol in totals.items()
            if any(by_symbol.values())
        }
