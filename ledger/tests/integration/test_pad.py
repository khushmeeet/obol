"""Pad directives (design §8, plan §4.2), on both backends.

The consumption rules were pinned against Beancount 3.2.3: a pad is spent
by the first assertion evaluated on its account after its date, whether or
not padding was needed; only the latest eligible pad generates; a pad
dated the same day as the assertion cannot serve it (the assertion checks
the start of the day); and the generated transaction is dated on the
pad's date, flagged 'P'.
"""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.directives import AssertionStatus
from ledger.domain.errors import (
    AccountNotOpenError,
    PadError,
    UnknownAccountError,
)
from ledger.domain.transaction import PostingSpec, TransactionSpec

JAN1 = datetime.date(2024, 1, 1)


@pytest.fixture
def led(ledger):
    ledger.create_commodity("USD", "currency")
    ledger.create_account("Assets:Checking", "asset", JAN1)
    ledger.create_account("Equity:Opening-Balances", "equity", JAN1)
    ledger.create_account("Income:Salary", "income", JAN1)
    return ledger


def deposit(led, day, amount, account="Assets:Checking"):
    from ledger.domain.amount import Amount

    usd = led.get_commodity("USD")
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, day),
            postings=[
                PostingSpec(
                    account=account,
                    units=Amount.from_decimal(Decimal(amount), usd),
                ),
                PostingSpec(account="Income:Salary"),
            ],
        )
    )


def test_pad_generates_the_balancing_transaction(led):
    """The mid-life connection shape: the account existed before Obol did;
    pad it against opening balances and assert the reported balance."""
    pad = led.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)
    assert pad.id is not None
    assert pad.consumed_by is None and pad.generated_txn_id is None

    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("1234.56"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS
    assert assertion.difference.value == 0

    stored_pad = led.list_pads()[0]
    assert stored_pad.consumed_by == assertion.id
    assert stored_pad.generated_txn_id is not None

    generated = led.get_transaction(stored_pad.generated_txn_id)
    assert generated.date == JAN1  # dated on the pad, not the assertion
    assert generated.flag == "P"
    assert generated.generated
    assert generated.source == "pad"
    assert generated.source_ref == str(stored_pad.id)
    by_account = {p.account: p.units.to_decimal() for p in generated.postings}
    assert by_account == {
        "Assets:Checking": Decimal("1234.56"),
        "Equity:Opening-Balances": Decimal("-1234.56"),
    }

    assert led.balance("Assets:Checking").to_dict() == {"USD": Decimal("1234.56")}
    assert led.balance("Equity:Opening-Balances").to_dict() == {
        "USD": Decimal("-1234.56")
    }


def test_pad_tops_up_existing_history(led):
    deposit(led, 2, "200.00")
    led.pad("Assets:Checking", "Equity:Opening-Balances", datetime.date(2024, 1, 3))
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 10), Decimal("500.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS
    generated = led.get_transaction(led.list_pads()[0].generated_txn_id)
    by_account = {p.account: p.units.to_decimal() for p in generated.postings}
    assert by_account["Assets:Checking"] == Decimal("300.00")


def test_pad_can_pad_downward(led):
    deposit(led, 2, "500.00")
    led.pad("Assets:Checking", "Equity:Opening-Balances", datetime.date(2024, 1, 3))
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 10), Decimal("450.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS
    generated = led.get_transaction(led.list_pads()[0].generated_txn_id)
    by_account = {p.account: p.units.to_decimal() for p in generated.postings}
    assert by_account["Assets:Checking"] == Decimal("-50.00")
    assert by_account["Equity:Opening-Balances"] == Decimal("50.00")


def test_pad_is_spent_even_when_nothing_was_needed(led):
    """Beancount rule: a pad whose next assertion already passes is
    "unused" — spent without generating. It must not stay armed and
    retroactively break that assertion by serving a later one."""
    deposit(led, 2, "500.00")
    led.pad("Assets:Checking", "Equity:Opening-Balances", datetime.date(2024, 1, 3))
    first = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("500.00"), "USD"
    )
    assert first.status is AssertionStatus.PASS
    pad = led.list_pads()[0]
    assert pad.consumed_by == first.id
    assert pad.generated_txn_id is None

    second = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 10), Decimal("600.00"), "USD"
    )
    assert second.status is AssertionStatus.FAIL  # the pad did not revive


def test_pad_serves_only_the_next_assertion(led):
    led.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)
    first = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("500.00"), "USD"
    )
    assert first.status is AssertionStatus.PASS
    second = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 10), Decimal("999.00"), "USD"
    )
    assert second.status is AssertionStatus.FAIL
    assert second.difference.to_decimal() == Decimal("-499.00")


def test_latest_of_several_pads_generates(led):
    """Two pads with no assertion between them: the later one wins; the
    earlier is spent without effect (Beancount's "unused pad")."""
    led.pad("Assets:Checking", "Equity:Opening-Balances", datetime.date(2024, 1, 2))
    led.pad("Assets:Checking", "Equity:Opening-Balances", datetime.date(2024, 1, 3))
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("100.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS
    first, second = led.list_pads()
    assert first.consumed_by == assertion.id and first.generated_txn_id is None
    assert second.consumed_by == assertion.id and second.generated_txn_id is not None
    generated = led.get_transaction(second.generated_txn_id)
    assert generated.date == datetime.date(2024, 1, 3)


def test_same_day_pad_cannot_serve_the_assertion(led):
    """The assertion checks the start of its date; a padding transaction
    dated that same day would land after it (verified Beancount rule:
    same-day pad stays unused and the balance check fails)."""
    day = datetime.date(2024, 1, 5)
    led.pad("Assets:Checking", "Equity:Opening-Balances", day)
    assertion = led.assert_balance("Assets:Checking", day, Decimal("500.00"), "USD")
    assert assertion.status is AssertionStatus.FAIL
    pad = led.list_pads()[0]
    assert pad.consumed_by is None  # still armed, for a later assertion
    later = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 6), Decimal("500.00"), "USD"
    )
    assert later.status is AssertionStatus.PASS
    assert led.list_pads()[0].consumed_by == later.id


def test_pad_matches_its_account_exactly(led):
    """Deliberate divergence from Beancount, where a pad on the parent is
    consumed by a child's balance check yet still fails it: Obol pads are
    consumed only by assertions on exactly the padded account."""
    led.create_account("Assets:Checking:Sub", "asset", JAN1)
    led.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)
    child = led.assert_balance(
        "Assets:Checking:Sub", datetime.date(2024, 1, 5), Decimal("200.00"), "USD"
    )
    assert child.status is AssertionStatus.FAIL
    assert led.list_pads()[0].consumed_by is None

    parent = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("300.00"), "USD"
    )
    assert parent.status is AssertionStatus.PASS
    assert led.list_pads()[0].consumed_by == parent.id


def test_check_assertions_reconciles_out_of_order_entry(led):
    """An assertion stored before its pad exists fails at entry time;
    check_assertions() re-evaluates with the pad machinery active."""
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("750.00"), "USD"
    )
    assert assertion.status is AssertionStatus.FAIL
    led.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)

    rechecked = led.check_assertions()
    assert rechecked[0].status is AssertionStatus.PASS
    assert led.balance("Assets:Checking").to_dict() == {"USD": Decimal("750.00")}

    # Idempotent: nothing new generated on a second run.
    led.check_assertions()
    transactions = led._repo.list_transactions()
    assert len(transactions) == 1
    assert transactions[0].generated


def test_pad_validation_errors(led):
    with pytest.raises(PadError):
        led.pad("Assets:Checking", "Assets:Checking", JAN1)
    with pytest.raises(UnknownAccountError):
        led.pad("Assets:Nowhere", "Equity:Opening-Balances", JAN1)
    with pytest.raises(UnknownAccountError):
        led.pad("Assets:Checking", "Equity:Nowhere", JAN1)
    with pytest.raises(AccountNotOpenError):
        led.pad(
            "Assets:Checking", "Equity:Opening-Balances", datetime.date(2023, 12, 31)
        )


def test_padding_survives_in_journal_and_statements(led):
    """Padding is visible and filterable: flagged 'P', generated, source
    'pad' — and it lands in balances like any other transaction."""
    led.pad("Assets:Checking", "Equity:Opening-Balances", JAN1)
    led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 5), Decimal("1000.00"), "USD"
    )
    entries = led.journal("Assets:Checking")
    assert len(entries) == 1
    assert entries[0].flag == "P"
    sheet = led.balance_sheet()
    assert sheet.net_worth.to_dict() == {"USD": Decimal("1000.00")}
