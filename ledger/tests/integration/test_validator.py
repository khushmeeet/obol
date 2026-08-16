"""The validator (design §12, plan §4.3).

A validator that has never caught anything is not known to work: each
corruption test deliberately breaks one invariant — through raw SQL on the
SQLite backend, since the repository refuses such writes, after dropping
the M5 append-only triggers that refuse them too — and asserts the
specific check reports it. Clean-ledger tests run on both backends.
"""

import datetime
from decimal import Decimal

import pytest
from scenarios import build_month_of_spending, build_one_paycheck, build_transfer
from test_append_only import drop_append_only_triggers

from ledger.api import Ledger
from ledger.domain.directives import AssertionStatus
from ledger.domain.transaction import PostingSpec, TransactionSpec
from ledger.storage.db import connect

JAN1 = datetime.date(2024, 1, 1)


# --- clean ledgers validate clean (both backends) --------------------------


@pytest.mark.parametrize(
    "build", [build_month_of_spending, build_one_paycheck, build_transfer]
)
def test_golden_scenarios_validate_clean(ledger, build):
    report = build(ledger).validate()
    assert report.ok, str(report)


def test_ledger_with_assertions_and_pad_validates_clean(ledger):
    ledger.create_commodity("USD", "currency")
    ledger.create_account("Assets:Checking", "asset", JAN1)
    ledger.create_account("Equity:Opening-Balances", "equity", JAN1)
    ledger.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)
    ledger.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("800.00"), "USD"
    )
    report = ledger.validate()
    assert report.ok, str(report)


def test_failing_assertion_is_reported_but_stale_status_is_not_invented(ledger):
    ledger.create_commodity("USD", "currency")
    ledger.create_account("Assets:Checking", "asset", JAN1)
    assertion = ledger.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("10.00"), "USD"
    )
    assert assertion.status is AssertionStatus.FAIL
    report = ledger.validate()
    failures = report.by_check().get("balance-assertion", [])
    assert len(failures) == 1  # the failure itself; the stored row is fresh
    assert failures[0].assertion_id == assertion.id


def test_stale_assertion_status_is_a_finding(ledger):
    """Both backends: a stored result that no longer matches a fresh
    recomputation is reported (here it went stale because a transaction
    was backfilled before the assertion date)."""
    ledger.create_commodity("USD", "currency")
    ledger.create_account("Assets:Checking", "asset", JAN1)
    ledger.create_account("Income:Salary", "income", JAN1)
    assertion = ledger.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 10), Decimal("0.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS

    from ledger.domain.amount import Amount

    usd = ledger.get_commodity("USD")
    ledger.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 2),
            postings=[
                PostingSpec(
                    account="Assets:Checking",
                    units=Amount.from_decimal(Decimal("100.00"), usd),
                ),
                PostingSpec(account="Income:Salary"),
            ],
        )
    )
    findings = ledger.validate().by_check().get("balance-assertion", [])
    messages = " ".join(f.message for f in findings)
    assert "stale" in messages
    assert any(
        f.message.startswith(f"assertion {assertion.id} fails") for f in findings
    )

    ledger.check_assertions()
    findings = ledger.validate().by_check().get("balance-assertion", [])
    assert all("stale" not in f.message for f in findings)  # failure remains, fresh


# --- corruption injection (SQLite; raw SQL bypasses the repository) --------


@pytest.fixture
def sqlite_ledger(tmp_path):
    led = Ledger.open(tmp_path / "ledger.db")
    build_month_of_spending(led)
    drop_append_only_triggers(led._conn)  # the raw-rewrite escape hatch
    yield led
    led.close()


def conn_of(led: Ledger):
    return led._conn


def test_flipped_sign_is_caught(sqlite_ledger):
    conn_of(sqlite_ledger).execute(
        "UPDATE postings SET units = -units, weight = -weight"
        " WHERE id = (SELECT MIN(id) FROM postings)"
    )
    report = sqlite_ledger.validate()
    checks = set(report.by_check())
    assert "transaction-balance" in checks
    assert "global-balance" in checks


def test_deleted_posting_is_caught(sqlite_ledger):
    conn_of(sqlite_ledger).execute(
        "DELETE FROM postings WHERE id = (SELECT MIN(id) FROM postings)"
    )
    report = sqlite_ledger.validate()
    checks = set(report.by_check())
    assert "minimum-postings" in checks
    assert "transaction-balance" in checks


def test_backdated_transaction_is_caught(sqlite_ledger):
    conn_of(sqlite_ledger).execute(
        "UPDATE transactions SET date = '2023-06-15'"
        " WHERE id = (SELECT MIN(id) FROM transactions)"
    )
    findings = sqlite_ledger.validate().by_check()["account-lifetime"]
    assert findings


def test_doctored_weight_is_caught(sqlite_ledger):
    conn_of(sqlite_ledger).execute(
        "UPDATE postings SET weight = weight + 1"
        " WHERE id = (SELECT MIN(id) FROM postings)"
    )
    checks = set(sqlite_ledger.validate().by_check())
    assert "weight-consistency" in checks


def test_double_interpolation_is_caught(sqlite_ledger):
    conn_of(sqlite_ledger).execute(
        "UPDATE postings SET interpolated = 1"
        " WHERE transaction_id = (SELECT MIN(id) FROM transactions)"
    )
    assert "interpolation" in set(sqlite_ledger.validate().by_check())


def test_disallowed_commodity_is_caught(tmp_path):
    with Ledger.open(tmp_path / "constrained.db") as led:
        led.create_commodity("USD", "currency")
        led.create_commodity("CAD", "currency")
        led.create_account(
            "Assets:Checking", "asset", JAN1, allowed_commodities=["USD"]
        )
        led.create_account("Income:Salary", "income", JAN1)
        from ledger.domain.amount import Amount

        usd = led.get_commodity("USD")
        led.record(
            TransactionSpec(
                date=datetime.date(2024, 1, 2),
                postings=[
                    PostingSpec(
                        account="Assets:Checking",
                        units=Amount.from_decimal(Decimal("10.00"), usd),
                    ),
                    PostingSpec(account="Income:Salary"),
                ],
            )
        )
        drop_append_only_triggers(led._conn)
        led._conn.execute(
            "UPDATE postings SET commodity_id ="
            " (SELECT id FROM commodities WHERE symbol = 'CAD')"
            " WHERE account_id ="
            " (SELECT id FROM accounts WHERE path = 'Assets:Checking')"
        )
        findings = led.validate().by_check()["allowed-commodities"]
        assert findings[0].account == "Assets:Checking"


def test_broken_pad_trace_is_caught(tmp_path):
    with Ledger.open(tmp_path / "pad.db") as led:
        led.create_commodity("USD", "currency")
        led.create_account("Assets:Checking", "asset", JAN1)
        led.create_account("Equity:Opening-Balances", "equity", JAN1)
        led.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)
        led.assert_balance(
            "Assets:Checking", datetime.date(2024, 1, 5), Decimal("100.00"), "USD"
        )
        assert led.validate().ok
        drop_append_only_triggers(led._conn)
        led._conn.execute("UPDATE transactions SET source_ref = '999'")
        findings = led.validate().by_check()["generated-trace"]
        assert findings


def test_orphaned_parent_link_is_caught(sqlite_ledger):
    conn_of(sqlite_ledger).execute(
        "UPDATE accounts SET parent_id ="
        " (SELECT id FROM accounts WHERE path = 'Expenses:Food:Groceries')"
        " WHERE path = 'Assets:Checking'"
    )
    findings = sqlite_ledger.validate().by_check()["storage-integrity"]
    assert "Assets:Checking" in findings[0].message


def test_dangling_foreign_key_is_caught(tmp_path):
    """foreign_keys is a connection pragma, not a table constraint: a
    tool writing without it can leave dangling rows the validator must
    still see."""
    path = tmp_path / "dangling.db"
    with Ledger.open(path) as led:
        build_month_of_spending(led)
    raw = connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    drop_append_only_triggers(raw)
    raw.execute(
        "DELETE FROM transactions WHERE id = (SELECT MIN(id) FROM transactions)"
    )
    raw.close()
    with Ledger.open(path) as led:
        findings = led.validate().by_check()["storage-integrity"]
        assert any("dangling" in f.message for f in findings)


def test_dangling_tag_row_is_caught(tmp_path):
    """The M6 tables are covered by the same storage check: a
    transaction_tags row pointing at a transaction that does not exist is
    corruption."""
    path = tmp_path / "dangling_tag.db"
    with Ledger.open(path) as led:
        build_month_of_spending(led)
    raw = connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("INSERT INTO tags (name) VALUES ('orphan')")
    raw.execute(
        "INSERT INTO transaction_tags (transaction_id, tag_id)"
        " VALUES (999, (SELECT id FROM tags WHERE name = 'orphan'))"
    )
    raw.close()
    with Ledger.open(path) as led:
        findings = led.validate().by_check()["storage-integrity"]
        assert any(
            "dangling" in f.message and "transaction_tags" in f.message
            for f in findings
        )


@pytest.fixture
def reversed_ledger(tmp_path):
    """A ledger holding one transaction and its reversal, triggers
    dropped, ready for corruption."""
    led = Ledger.open(tmp_path / "reversed.db")
    led.create_commodity("USD", "currency")
    led.create_account("Assets:Checking", "asset", JAN1)
    led.create_account("Expenses:Food", "expense", JAN1)
    from ledger.domain.amount import Amount

    usd = led.get_commodity("USD")
    original = led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            postings=[
                PostingSpec(
                    account="Assets:Checking",
                    units=Amount.from_decimal(Decimal("-20.00"), usd),
                ),
                PostingSpec(account="Expenses:Food"),
            ],
        )
    )
    reversal = led.reverse(original.id, datetime.date(2024, 1, 9), "test")
    assert led.validate().ok
    drop_append_only_triggers(led._conn)
    yield led, original, reversal
    led.close()


def test_doctored_reversal_postings_are_caught(reversed_ledger):
    """Doubling both legs keeps the reversal internally balanced, so only
    the exact-negation rule can see it."""
    led, _original, reversal = reversed_ledger
    led._conn.execute(
        "UPDATE postings SET units = units * 2, weight = weight * 2"
        " WHERE transaction_id = ?",
        (reversal.id,),
    )
    findings = led.validate().by_check()["reversal-trace"]
    assert any("does not exactly negate" in f.message for f in findings)


def test_reversal_without_reverses_id_is_caught(reversed_ledger):
    led, _original, reversal = reversed_ledger
    led._conn.execute(
        "UPDATE transactions SET reverses_id = NULL WHERE id = ?", (reversal.id,)
    )
    findings = led.validate().by_check()["reversal-trace"]
    assert any("carries no reverses_id" in f.message for f in findings)


def test_backdated_reversal_is_caught(reversed_ledger):
    led, _original, reversal = reversed_ledger
    led._conn.execute(
        "UPDATE transactions SET date = '2024-01-03' WHERE id = ?", (reversal.id,)
    )
    findings = led.validate().by_check()["reversal-trace"]
    assert any("never backdated" in f.message for f in findings)


def test_replacement_whose_reversal_is_missing_is_caught(reversed_ledger):
    """Pointing a second transaction's reverses_id at an unreversed
    original claims a replacement whose reversal never happened — a
    double-counted effect."""
    led, original, reversal = reversed_ledger
    led._conn.execute(
        "UPDATE transactions SET source = NULL, source_ref = NULL, reverses_id = ?"
        " WHERE id = ?",
        (reversal.id, original.id),
    )
    findings = led.validate().by_check()["reversal-trace"]
    assert any("has no reversal" in f.message for f in findings)


# --- lot corruption (M7: design §12 checks 7-9) -----------------------------


@pytest.fixture
def lot_ledger(tmp_path):
    """A ledger holding two lots and a partial FIFO sale, triggers
    dropped, ready for corruption."""
    led = Ledger.open(tmp_path / "lots.db")
    led.create_commodity("USD", "currency")
    led.create_commodity("AAPL", "security", 0)
    led.set_option("gains_account_root", "Income:Gains")
    led.create_account("Assets:Cash", "asset", JAN1)
    led.create_account("Assets:Brokerage", "asset", JAN1, booking_method="FIFO")
    led.create_account("Equity:Opening-Balances", "equity", JAN1)
    from ledger.domain.amount import Amount
    from ledger.domain.transaction import CostSpec

    usd = led.get_commodity("USD")
    aapl = led.get_commodity("AAPL")

    def amount(text, commodity=usd):
        return Amount.from_decimal(Decimal(text), commodity)

    led.record(
        TransactionSpec(
            date=JAN1,
            postings=[
                PostingSpec("Assets:Cash", units=amount("5000.00")),
                PostingSpec("Equity:Opening-Balances", units=amount("-5000.00")),
            ],
        )
    )
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            postings=[
                PostingSpec(
                    "Assets:Brokerage",
                    units=amount("10", aapl),
                    cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
                ),
                PostingSpec("Assets:Cash", units=amount("-1000.00")),
            ],
        )
    )
    sale = led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 10),
            postings=[
                PostingSpec(
                    "Assets:Brokerage",
                    units=amount("-4", aapl),
                    cost=CostSpec(),
                    price=amount("130.00"),
                ),
                PostingSpec("Assets:Cash", units=amount("520.00")),
                PostingSpec("Income:Gains:Brokerage", units=None),
            ],
        )
    )
    assert led.validate().ok
    drop_append_only_triggers(led._conn)
    yield led, sale
    led.close()


def test_doctored_reduction_quantity_is_caught(lot_ledger):
    led, _sale = lot_ledger
    led._conn.execute("UPDATE lot_reductions SET quantity = quantity + 100000000")
    checks = led.validate().by_check()
    # the posting's matches no longer sum to its units, and the stored
    # weight no longer matches the recomputation
    assert "lot-trace" in checks
    assert "weight-consistency" in checks


def test_overconsumed_lot_is_caught(lot_ledger):
    led, _sale = lot_ledger
    # consume 14 of an original 10 (design §12 checks 7-8)
    led._conn.execute("UPDATE lot_reductions SET quantity = 1400000000")
    findings = led.validate().by_check()["lot-reduction"]
    assert any("reaches consumed quantity" in f.message for f in findings)


def test_doctored_lot_cost_is_caught(lot_ledger):
    led, _sale = lot_ledger
    led._conn.execute("UPDATE lots SET cost_per_unit = cost_per_unit + 1")
    checks = led.validate().by_check()
    assert "lot-trace" in checks  # disagrees with its opening posting
    assert "weight-consistency" in checks  # sale weight no longer derivable


def test_doctored_lot_quantity_is_caught(lot_ledger):
    led, _sale = lot_ledger
    led._conn.execute(
        "UPDATE lots SET original_quantity = original_quantity - 100000000"
    )
    findings = led.validate().by_check()["lot-trace"]
    assert any("disagrees with its opening posting" in f.message for f in findings)


def test_missing_lot_row_is_caught(lot_ledger):
    led, _sale = lot_ledger
    led._conn.execute("PRAGMA foreign_keys = OFF")
    led._conn.execute("DELETE FROM lots")
    checks = led.validate().by_check()
    assert "storage-integrity" in checks  # dangling lot_reductions rows
    assert "weight-consistency" in checks  # matches reference a missing lot
    assert "lot-trace" in checks  # the acquisition opened no lot


def test_reduction_predating_its_lot_is_caught(lot_ledger):
    led, sale = lot_ledger
    led._conn.execute(
        "UPDATE transactions SET date = '2024-01-03' WHERE id = ?", (sale.id,)
    )
    findings = led.validate().by_check()["lot-reduction"]
    assert any("predates lot" in f.message for f in findings)


def test_stripped_restoration_is_caught(lot_ledger):
    """A reversal whose lot restorations were deleted no longer negates
    the sale: dropping the rows also erases the empty-{} cost marker, so
    the exact-negation rule sees a cost-less posting where the original
    had one."""
    led, sale = lot_ledger
    reversal = led.reverse(sale.id, datetime.date(2024, 1, 12), "unwinding")
    assert led.validate().ok
    led._conn.execute(
        "DELETE FROM lot_reductions WHERE quantity < 0 AND posting_id IN"
        " (SELECT id FROM postings WHERE transaction_id = ?)",
        (reversal.id,),
    )
    findings = led.validate().by_check()["reversal-trace"]
    assert any("does not exactly negate" in f.message for f in findings)


def test_doctored_restoration_quantity_is_caught(lot_ledger):
    """Shrinking (not deleting) a restoration keeps the posting
    cost-carrying but leaves the lot effects un-undone — the lot-specific
    reversal rule sees it."""
    led, sale = lot_ledger
    reversal = led.reverse(sale.id, datetime.date(2024, 1, 12), "unwinding")
    assert led.validate().ok
    led._conn.execute(
        "UPDATE lot_reductions SET quantity = quantity + 100000000"
        " WHERE quantity < 0 AND posting_id IN"
        " (SELECT id FROM postings WHERE transaction_id = ?)",
        (reversal.id,),
    )
    checks = led.validate().by_check()
    assert any(
        "does not undo the lot effects" in f.message for f in checks["reversal-trace"]
    )
    assert "lot-trace" in checks  # matches no longer sum to the units


def test_report_renders_and_counts(sqlite_ledger):
    assert str(sqlite_ledger.validate()) == "ok: all checks passed"
    conn_of(sqlite_ledger).execute(
        "UPDATE postings SET units = -units, weight = -weight"
        " WHERE id = (SELECT MIN(id) FROM postings)"
    )
    report = sqlite_ledger.validate()
    assert not report.ok
    rendered = str(report)
    assert "finding(s):" in rendered
    assert "[transaction-balance]" in rendered
