"""Balance assertions through the API (design §8, plan §4.1), on both
backends: store-then-check, start-of-date semantics, sub-account
accumulation, per-commodity independence, precision-derived tolerance,
and re-checking after new postings arrive."""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.directives import AssertionStatus
from ledger.domain.errors import (
    AccountNotOpenError,
    UnknownAccountError,
    UnknownCommodityError,
)
from ledger.domain.transaction import PostingSpec, TransactionSpec

JAN1 = datetime.date(2024, 1, 1)


@pytest.fixture
def led(ledger):
    ledger.create_commodity("USD", "currency")
    ledger.create_account("Assets:Checking", "asset", JAN1)
    ledger.create_account("Assets:Checking:Sub", "asset", JAN1)
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


def test_assertion_is_stored_and_checked(led):
    deposit(led, 2, "500.00")
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("500.00"), "USD"
    )
    assert assertion.id is not None
    assert assertion.status is AssertionStatus.PASS
    assert assertion.difference is not None and assertion.difference.value == 0
    assert assertion.checked_at is not None

    stored = led.get_assertion(assertion.id)
    assert stored is not None
    assert stored.status is AssertionStatus.PASS
    assert stored.checked_at == assertion.checked_at
    assert led.list_assertions() == [stored]
    assert led.list_assertions("Assets:Checking") == [stored]
    assert led.list_assertions("Assets:Checking:Sub") == []


def test_failure_is_data_not_an_exception(led):
    deposit(led, 2, "500.00")
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("450.00"), "USD"
    )
    assert assertion.status is AssertionStatus.FAIL
    assert assertion.difference.to_decimal() == Decimal("50.00")
    assert led.get_assertion(assertion.id).status is AssertionStatus.FAIL


def test_assertion_checks_start_of_date(led):
    """A posting dated the assertion day itself is not counted — the
    assertion holds at the start of that date (verified Beancount rule)."""
    deposit(led, 2, "500.00")
    deposit(led, 3, "100.00")
    same_day = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("500.00"), "USD"
    )
    assert same_day.status is AssertionStatus.PASS
    next_day = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 4), Decimal("600.00"), "USD"
    )
    assert next_day.status is AssertionStatus.PASS


def test_assertion_includes_sub_accounts(led):
    """Asserting the parent covers the whole subtree (verified Beancount
    rule) — what makes an institution-level Plaid balance meaningful when
    the product splits the account."""
    deposit(led, 2, "300.00")
    deposit(led, 2, "200.00", account="Assets:Checking:Sub")
    combined = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("500.00"), "USD"
    )
    assert combined.status is AssertionStatus.PASS
    parent_only = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("300.00"), "USD"
    )
    assert parent_only.status is AssertionStatus.FAIL


def test_assertions_are_per_commodity(led):
    """An assertion in one commodity says nothing about others held in the
    same account."""
    led.create_commodity("CAD", "currency")
    from ledger.domain.amount import Amount

    cad = led.get_commodity("CAD")
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 2),
            postings=[
                PostingSpec(
                    account="Assets:Checking",
                    units=Amount.from_decimal(Decimal("75.00"), cad),
                ),
                PostingSpec(account="Income:Salary"),
            ],
        )
    )
    deposit(led, 2, "500.00")
    usd_view = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("500.00"), "USD"
    )
    cad_view = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("75.00"), "CAD"
    )
    assert usd_view.status is AssertionStatus.PASS
    assert cad_view.status is AssertionStatus.PASS


def test_zero_assertion_on_empty_account_passes(led):
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 2), Decimal("0.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS


def test_tolerance_comes_from_the_asserted_precision(led):
    deposit(led, 2, "100.0008")
    three_decimals = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("100.000"), "USD"
    )
    assert three_decimals.status is AssertionStatus.PASS  # within 0.001
    four_decimals = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("100.0000"), "USD"
    )
    assert four_decimals.status is AssertionStatus.FAIL  # 0.0008 > 0.0001


def test_integer_assertion_is_exact(led):
    deposit(led, 2, "100.40")
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("100"), "USD"
    )
    assert assertion.status is AssertionStatus.FAIL


def test_multiplier_option_loosens_assertions(led):
    deposit(led, 2, "100.015")
    led.set_option("inferred_tolerance_multiplier", "2")
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 3), Decimal("100.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS


def test_unknown_account_and_commodity_are_rejected(led):
    with pytest.raises(UnknownAccountError):
        led.assert_balance(
            "Assets:Nowhere", datetime.date(2024, 1, 3), Decimal("1.00"), "USD"
        )
    with pytest.raises(UnknownCommodityError):
        led.assert_balance(
            "Assets:Checking", datetime.date(2024, 1, 3), Decimal("1.00"), "EUR"
        )


def test_assertion_before_account_opened_is_rejected(led):
    """Beancount reports a balance before the open date as a reference to
    an inactive account; an assertion dated after a close is fine."""
    with pytest.raises(AccountNotOpenError):
        led.assert_balance(
            "Assets:Checking", datetime.date(2023, 12, 31), Decimal("0.00"), "USD"
        )
    led.close_account("Assets:Checking:Sub", datetime.date(2024, 2, 1))
    after_close = led.assert_balance(
        "Assets:Checking:Sub", datetime.date(2024, 3, 1), Decimal("0.00"), "USD"
    )
    assert after_close.status is AssertionStatus.PASS


def test_check_assertions_refreshes_stale_results(led):
    deposit(led, 2, "500.00")
    assertion = led.assert_balance(
        "Assets:Checking", datetime.date(2024, 1, 10), Decimal("500.00"), "USD"
    )
    assert assertion.status is AssertionStatus.PASS

    # A backfilled transaction lands before the assertion date: the stored
    # result is now stale until re-checked.
    deposit(led, 5, "100.00")
    assert led.get_assertion(assertion.id).status is AssertionStatus.PASS

    rechecked = led.check_assertions()
    assert len(rechecked) == 1
    assert rechecked[0].status is AssertionStatus.FAIL
    assert rechecked[0].difference.to_decimal() == Decimal("100.00")
    assert led.get_assertion(assertion.id).status is AssertionStatus.FAIL


def test_source_is_stored(led):
    assertion = led.assert_balance(
        "Assets:Checking",
        datetime.date(2024, 1, 2),
        Decimal("0.00"),
        "USD",
        source="plaid",
    )
    assert led.get_assertion(assertion.id).source == "plaid"
