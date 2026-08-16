"""Append-only enforcement (design §11, plan §7): the database itself
refuses UPDATE and DELETE on transactions and postings, for any
connection — repository discipline is backed by BEFORE UPDATE / BEFORE
DELETE triggers from migration 0003 (since M6 also on tags,
transaction_tags, and links from migration 0004: a transaction's tags
are part of its committed record; and since M7 on lots and
lot_reductions from migration 0005: booking is resolved once and
stored). SQLite only; the in-memory backend simply has no update or
delete methods."""

import datetime
import sqlite3
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.amount import Amount
from ledger.domain.transaction import PostingSpec, TransactionSpec

JAN1 = datetime.date(2024, 1, 1)

APPEND_ONLY_TRIGGERS = [
    "transactions_append_only_update",
    "transactions_append_only_delete",
    "postings_append_only_update",
    "postings_append_only_delete",
    "tags_append_only_update",
    "tags_append_only_delete",
    "transaction_tags_append_only_update",
    "transaction_tags_append_only_delete",
    "links_append_only_update",
    "links_append_only_delete",
    "lots_append_only_update",
    "lots_append_only_delete",
    "lot_reductions_append_only_update",
    "lot_reductions_append_only_delete",
]


def drop_append_only_triggers(conn: sqlite3.Connection) -> None:
    for name in APPEND_ONLY_TRIGGERS:
        conn.execute(f"DROP TRIGGER {name}")


@pytest.fixture
def led(tmp_path):
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_commodity("USD", "currency")
        ledger.create_commodity("AAPL", "security", 0)
        ledger.set_option("gains_account_root", "Income:Gains")
        ledger.create_account("Assets:Checking", "asset", JAN1)
        ledger.create_account("Assets:Brokerage", "asset", JAN1)
        ledger.create_account("Expenses:Food", "expense", JAN1)
        ledger.create_account("Equity:Opening-Balances", "equity", JAN1)
        usd = ledger.get_commodity("USD")
        aapl = ledger.get_commodity("AAPL")
        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, 1, 5),
                postings=[
                    PostingSpec(
                        account="Assets:Checking",
                        units=Amount.from_decimal(Decimal("-10.00"), usd),
                    ),
                    PostingSpec(account="Expenses:Food"),
                ],
                tags={"groceries"},
                links={"receipt-1"},
            )
        )
        from ledger.domain.transaction import CostSpec

        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, 1, 6),
                postings=[
                    PostingSpec(
                        account="Assets:Brokerage",
                        units=Amount.from_decimal(Decimal("2"), aapl),
                        cost=CostSpec(per_unit=Decimal("5.00"), commodity="USD"),
                    ),
                    PostingSpec(
                        account="Assets:Checking",
                        units=Amount.from_decimal(Decimal("-10.00"), usd),
                    ),
                ],
            )
        )
        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, 1, 7),
                postings=[
                    PostingSpec(
                        account="Assets:Brokerage",
                        units=Amount.from_decimal(Decimal("-1"), aapl),
                        cost=CostSpec(),
                        price=Amount.from_decimal(Decimal("6.00"), usd),
                    ),
                    PostingSpec(
                        account="Assets:Checking",
                        units=Amount.from_decimal(Decimal("6.00"), usd),
                    ),
                    PostingSpec(account="Income:Gains"),
                ],
            )
        )
        yield ledger


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transactions SET narration = 'doctored'",
        "DELETE FROM transactions",
        "UPDATE postings SET units = units + 1",
        "DELETE FROM postings",
        "UPDATE tags SET name = 'renamed'",
        "DELETE FROM tags",
        "UPDATE transaction_tags SET tag_id = tag_id + 1",
        "DELETE FROM transaction_tags",
        "UPDATE links SET name = 'relinked'",
        "DELETE FROM links",
        "UPDATE lots SET original_quantity = original_quantity * 2",
        "DELETE FROM lots",
        "UPDATE lot_reductions SET quantity = quantity + 1",
        "DELETE FROM lot_reductions",
    ],
)
def test_raw_rewrites_are_refused(led: Ledger, statement: str):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        led._conn.execute(statement)
    assert led.validate().ok


def test_triggers_fire_for_any_connection(led: Ledger, tmp_path):
    """The triggers live in the schema, not the connection: a separate raw
    connection (even with foreign_keys off) is refused too."""
    raw = sqlite3.connect(tmp_path / "ledger.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM postings")
    raw.close()


def test_narrow_update_paths_stay_open(led: Ledger):
    """balance_assertions, pads, and accounts keep their legitimate
    writes: assertion re-checks, pad consumption, account closing."""
    led.pad("Assets:Checking", "Equity:Opening-Balances", datetime.date(2024, 1, 2))
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 8), Decimal("40.00"), "USD"
    )
    assert assertion.checked_at is not None  # update_assertion_check ran
    pad = led.list_pads()[0]
    assert pad.consumed_by == assertion.id  # consume_pad ran
    led.check_assertions()  # re-evaluation re-writes the rows
    led.close_account("Expenses:Food", datetime.date(2024, 1, 31))


def test_dropping_the_triggers_is_the_escape_hatch(led: Ledger):
    """Forensic tooling (and the corruption-injection tests) can drop the
    triggers and rewrite history — and the validator still catches what
    they break."""
    drop_append_only_triggers(led._conn)
    led._conn.execute(
        "UPDATE postings SET units = -units, weight = -weight"
        " WHERE id = (SELECT MIN(id) FROM postings)"
    )
    report = led.validate()
    assert not report.ok
    assert "transaction-balance" in report.by_check()
