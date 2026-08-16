"""M5 corrections (design §11): reverse() and replace() as append-only
reversals linked via reverses_id, on both backends.

The lot-interaction contract (reversing a lot-reducing posting must
reverse its lot_reductions; reversing a lot-creating posting must fail
while reductions exist) is written down here as xfail tests for M7 to
fill in.
"""

import datetime
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.errors import (
    AccountNotOpenError,
    AlreadyReversedError,
    DuplicateSourceError,
    InvalidTransactionError,
    ReversalError,
    UnbalancedTransactionError,
    UnknownTransactionError,
)
from ledger.domain.transaction import (
    REVERSAL_SOURCE,
    CostSpec,
    PostingSpec,
    TransactionSpec,
)

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
JAN1 = datetime.date(2024, 1, 1)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def leg(account: str, text: str | None, commodity: Commodity = USD) -> PostingSpec:
    units = A(text, commodity) if text is not None else None
    return PostingSpec(account=account, units=units)


def txn(day: int, *postings: PostingSpec, **kwargs) -> TransactionSpec:
    return TransactionSpec(
        date=datetime.date(2024, 1, day), postings=list(postings), **kwargs
    )


def day(n: int) -> datetime.date:
    return datetime.date(2024, 1, n)


def raw(ledger: Ledger, account: str, symbol: str = "USD") -> int:
    amount = ledger.balance(account).get(symbol)
    return amount.value if amount is not None else 0


def raw_on(ledger: Ledger, account: str, on: datetime.date, symbol: str = "USD") -> int:
    amount = ledger.balance(account, on).get(symbol)
    return amount.value if amount is not None else 0


@pytest.fixture
def led(ledger: Ledger) -> Ledger:
    ledger.create_commodity("USD", "currency")
    for path, type_ in [
        ("Assets:Checking", "asset"),
        ("Expenses:Food", "expense"),
        ("Expenses:Travel", "expense"),
        ("Income:Salary", "income"),
        ("Equity:Opening-Balances", "equity"),
    ]:
        ledger.create_account(path, type_, JAN1)
    return ledger


class TestReverse:
    def test_reversal_restores_balances_and_keeps_history(self, led: Ledger):
        led.record(
            txn(5, leg("Assets:Checking", "-40.00"), leg("Expenses:Food", "40.00"))
        )
        wrong = led.record(
            txn(10, leg("Assets:Checking", "-25.00"), leg("Expenses:Travel", "25.00"))
        )
        assert wrong.id is not None
        led.reverse(wrong.id, day(20), "duplicate charge")

        # From the reversal date onward, only the first transaction remains.
        assert raw(led, "Assets:Checking") == A("-40.00").value
        assert raw(led, "Expenses:Travel") == 0
        # Between the original and the reversal, history is intact.
        assert raw_on(led, "Expenses:Travel", day(15)) == A("25.00").value
        # The original is still queryable, unchanged.
        original = led.get_transaction(wrong.id)
        assert original is not None
        assert [p.units.value for p in original.postings] == [
            A("-25.00").value,
            A("25.00").value,
        ]

    def test_reversal_shape(self, led: Ledger):
        original = led.record(
            txn(
                5,
                leg("Income:Salary", "-100.00"),
                leg("Assets:Checking", "60.00"),
                leg("Expenses:Food", "40.00"),
                payee="Hooli",
            )
        )
        assert original.id is not None
        reversal = led.reverse(original.id, day(9), "entered twice")

        assert reversal.id is not None
        assert reversal.date == day(9)
        assert reversal.reverses_id == original.id
        assert reversal.source == REVERSAL_SOURCE
        assert reversal.source_ref == str(original.id)
        assert reversal.generated is False
        assert reversal.flag == "*"
        assert reversal.narration == (
            f"(Reversal of transaction {original.id}: entered twice)"
        )
        # Exact negation, posting for posting, in the original's order.
        assert [
            (p.account, p.units.value, p.weight.value) for p in reversal.postings
        ] == [(p.account, -p.units.value, -p.weight.value) for p in original.postings]
        assert all(not p.interpolated for p in reversal.postings)
        # Discoverable through the source index.
        found = led.get_transaction_by_source(REVERSAL_SOURCE, str(original.id))
        assert found is not None and found.id == reversal.id

    def test_reason_is_optional(self, led: Ledger):
        original = led.record(
            txn(5, leg("Assets:Checking", "-1.00"), leg("Expenses:Food", "1.00"))
        )
        reversal = led.reverse(original.id, day(5))
        assert reversal.narration == f"(Reversal of transaction {original.id})"

    def test_interpolated_leg_reverses_as_stored(self, led: Ledger):
        original = led.record(
            txn(5, leg("Assets:Checking", "-33.10"), leg("Expenses:Food", None))
        )
        reversal = led.reverse(original.id, day(6), "wrong account")
        assert [p.units.value for p in reversal.postings] == [
            A("33.10").value,
            A("-33.10").value,
        ]
        assert raw(led, "Expenses:Food") == 0

    def test_unknown_transaction_rejected(self, led: Ledger):
        with pytest.raises(UnknownTransactionError):
            led.reverse(999, day(5))

    def test_double_reversal_rejected(self, led: Ledger):
        original = led.record(
            txn(5, leg("Assets:Checking", "-1.00"), leg("Expenses:Food", "1.00"))
        )
        led.reverse(original.id, day(6))
        with pytest.raises(AlreadyReversedError):
            led.reverse(original.id, day(7))

    def test_reversing_a_reversal_rejected(self, led: Ledger):
        original = led.record(
            txn(5, leg("Assets:Checking", "-1.00"), leg("Expenses:Food", "1.00"))
        )
        reversal = led.reverse(original.id, day(6))
        with pytest.raises(ReversalError, match="itself a reversal"):
            led.reverse(reversal.id, day(7))

    def test_reversing_generated_padding_rejected(self, led: Ledger):
        pad = led.pad("Assets:Checking", "Equity:Opening-Balances", day(2))
        led.assert_balance("Assets:Checking", day(5), Decimal("500.00"), "USD")
        padding = led.get_transaction_by_source("pad", str(pad.id))
        assert padding is not None and padding.generated
        with pytest.raises(ReversalError, match="machine-generated"):
            led.reverse(padding.id, day(6))

    def test_backdated_reversal_rejected_same_day_allowed(self, led: Ledger):
        original = led.record(
            txn(10, leg("Assets:Checking", "-1.00"), leg("Expenses:Food", "1.00"))
        )
        with pytest.raises(ReversalError, match="never backdated"):
            led.reverse(original.id, day(9))
        led.reverse(original.id, day(10))  # same day is fine
        assert raw(led, "Expenses:Food") == 0

    def test_reversal_into_closed_account_rejected(self, led: Ledger):
        original = led.record(
            txn(5, leg("Assets:Checking", "-1.00"), leg("Expenses:Food", "1.00"))
        )
        led.close_account("Expenses:Food", day(10))
        with pytest.raises(AccountNotOpenError):
            led.reverse(original.id, day(20))

    def test_dedup_survives_reversal(self, led: Ledger):
        """Reversing an ingested transaction must not reopen its dedup key:
        the original keeps (source, source_ref) forever."""
        spec = txn(
            5,
            leg("Assets:Checking", "-12.30"),
            leg("Expenses:Food", "12.30"),
            source="plaid",
            source_ref="txn_1",
        )
        original = led.record(spec)
        led.reverse(original.id, day(6), "bank removed it")
        again = txn(
            5,
            leg("Assets:Checking", "-12.30"),
            leg("Expenses:Food", "12.30"),
            source="plaid",
            source_ref="txn_1",
        )
        with pytest.raises(DuplicateSourceError):
            led.record(again)

    def test_reserved_sources_rejected_at_record(self, led: Ledger):
        for source in ("reversal", "pad"):
            with pytest.raises(InvalidTransactionError, match="reserved"):
                led.record(
                    txn(
                        5,
                        leg("Assets:Checking", "-1.00"),
                        leg("Expenses:Food", "1.00"),
                        source=source,
                        source_ref="x",
                    )
                )

    def test_validator_clean_after_reversal(self, led: Ledger):
        original = led.record(
            txn(5, leg("Assets:Checking", "-40.00"), leg("Expenses:Food", None))
        )
        led.reverse(original.id, day(8), "wrong category")
        report = led.validate()
        assert report.ok, str(report)


class TestReplace:
    def pending(self, **kwargs) -> TransactionSpec:
        return txn(
            5,
            leg("Assets:Checking", "-12.30"),
            leg("Expenses:Food", "12.30"),
            source="plaid",
            source_ref="pending-1",
            **kwargs,
        )

    def posted(self, **kwargs) -> TransactionSpec:
        return txn(
            8,
            leg("Assets:Checking", "-13.05"),
            leg("Expenses:Food", "13.05"),
            source="plaid",
            source_ref="posted-1",
            **kwargs,
        )

    def test_pending_to_posted_revision(self, led: Ledger):
        """The Plaid shape the design names: a pending amount is revised
        when the transaction posts under a new source id."""
        original = led.record(self.pending())
        replacement = led.replace(original.id, self.posted())

        assert replacement.id is not None
        assert replacement.reverses_id == original.id
        assert replacement.source_ref == "posted-1"
        # Net effect is the posted amount alone.
        assert raw(led, "Assets:Checking") == A("-13.05").value
        assert raw(led, "Expenses:Food") == A("13.05").value
        # History between the pending date and the revision is intact.
        assert raw_on(led, "Expenses:Food", day(6)) == A("12.30").value
        # The reversal exists, dated on the replacement's date by default.
        reversal = led.get_transaction_by_source(REVERSAL_SOURCE, str(original.id))
        assert reversal is not None
        assert reversal.date == day(8)
        assert reversal.reverses_id == original.id
        report = led.validate()
        assert report.ok, str(report)

    def test_explicit_reversal_date(self, led: Ledger):
        original = led.record(self.pending())
        led.replace(original.id, self.posted(), on_date=day(12))
        reversal = led.get_transaction_by_source(REVERSAL_SOURCE, str(original.id))
        assert reversal is not None and reversal.date == day(12)

    def test_both_ingestion_keys_stay_deduplicated(self, led: Ledger):
        original = led.record(self.pending())
        led.replace(original.id, self.posted())
        with pytest.raises(DuplicateSourceError):
            led.record(self.pending())
        with pytest.raises(DuplicateSourceError):
            led.record(self.posted())

    def test_replacement_reusing_the_original_ref_writes_nothing(self, led: Ledger):
        original = led.record(self.pending())
        same_ref = self.pending()
        same_ref.postings[0].units = A("-13.05")
        same_ref.postings[1].units = A("13.05")
        with pytest.raises(DuplicateSourceError, match="its own source_ref"):
            led.replace(original.id, same_ref)
        # Nothing landed: no reversal, balances unchanged.
        assert led.get_transaction_by_source(REVERSAL_SOURCE, str(original.id)) is None
        assert raw(led, "Assets:Checking") == A("-12.30").value

    def test_invalid_replacement_writes_nothing(self, led: Ledger):
        """The reversal and the replacement land together or not at all —
        checked on both backends, where in-memory has no rollback."""
        original = led.record(self.pending())
        unbalanced = txn(
            8,
            leg("Assets:Checking", "-13.05"),
            leg("Expenses:Food", "12.00"),
        )
        with pytest.raises(UnbalancedTransactionError):
            led.replace(original.id, unbalanced)
        assert led.get_transaction_by_source(REVERSAL_SOURCE, str(original.id)) is None
        assert raw(led, "Assets:Checking") == A("-12.30").value
        report = led.validate()
        assert report.ok, str(report)

    def test_replace_guards_match_reverse(self, led: Ledger):
        original = led.record(self.pending())
        reversal = led.reverse(original.id, day(6))
        with pytest.raises(AlreadyReversedError):
            led.replace(original.id, self.posted())
        with pytest.raises(ReversalError, match="itself a reversal"):
            led.replace(reversal.id, self.posted())
        with pytest.raises(UnknownTransactionError):
            led.replace(999, self.posted())

    def test_backdated_default_reversal_date_rejected(self, led: Ledger):
        original = led.record(self.pending())
        earlier = txn(
            3,
            leg("Assets:Checking", "-13.05"),
            leg("Expenses:Food", "13.05"),
        )
        with pytest.raises(ReversalError, match="never backdated"):
            led.replace(original.id, earlier)
        # An explicit, honest discovery date makes the same revision fine.
        led.replace(original.id, earlier, on_date=day(9))
        assert raw_on(led, "Expenses:Food", day(4)) == A("13.05").value

    def test_replacement_chain(self, led: Ledger):
        """A replacement is an ordinary transaction: it can itself be
        replaced, and the lineage stays walkable via reverses_id."""
        first = led.record(self.pending())
        second = led.replace(first.id, self.posted())
        final_spec = txn(
            10,
            leg("Assets:Checking", "-14.00"),
            leg("Expenses:Food", "14.00"),
        )
        third = led.replace(second.id, final_spec)
        assert third.reverses_id == second.id
        assert second.reverses_id == first.id
        assert raw(led, "Expenses:Food") == A("14.00").value
        report = led.validate()
        assert report.ok, str(report)

    def test_oracle_agrees_after_corrections(
        self, led: Ledger, assert_matches_beancount
    ):
        """Reversals and replacements are ordinary transactions in the
        export; Beancount computes the same balances from them."""
        led.record(txn(4, leg("Assets:Checking", "-40.00"), leg("Expenses:Food", None)))
        wrong = led.record(
            txn(5, leg("Assets:Checking", "-25.00"), leg("Expenses:Travel", "25.00"))
        )
        led.reverse(wrong.id, day(6), "duplicate")
        pending = led.record(self.pending())
        led.replace(pending.id, self.posted())
        assert_matches_beancount(led)


# --- the M7 lot-interaction contract, written down now (plan §7) -----------


def _brokerage(led: Ledger) -> Ledger:
    led.create_commodity("AAPL", "security", 0)
    led.create_account("Assets:Brokerage", "asset", JAN1)
    led.create_account("Income:Gains", "income", JAN1)
    return led


def test_reversing_a_reduction_restores_the_lot(led: Ledger):
    aapl = Commodity("AAPL", CommodityKind.SECURITY, 0)
    _brokerage(led)
    led.record(
        txn(
            5,
            PostingSpec(
                account="Assets:Brokerage",
                units=A("10", aapl),
                cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
            ),
            leg("Assets:Checking", "-1000.00"),
        )
    )
    sale = led.record(
        txn(
            10,
            PostingSpec(
                account="Assets:Brokerage",
                units=A("-4", aapl),
                cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
                price=A("110.00"),
            ),
            leg("Assets:Checking", "440.00"),
            leg("Income:Gains", None),
        )
    )
    led.reverse(sale.id, day(11), "broker bust")
    assert raw(led, "Assets:Brokerage", "AAPL") == A("10", aapl).value


def test_reversing_a_reduced_acquisition_fails(led: Ledger):
    aapl = Commodity("AAPL", CommodityKind.SECURITY, 0)
    _brokerage(led)
    buy = led.record(
        txn(
            5,
            PostingSpec(
                account="Assets:Brokerage",
                units=A("10", aapl),
                cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
            ),
            leg("Assets:Checking", "-1000.00"),
        )
    )
    led.record(
        txn(
            10,
            PostingSpec(
                account="Assets:Brokerage",
                units=A("-4", aapl),
                cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
                price=A("110.00"),
            ),
            leg("Assets:Checking", "440.00"),
            leg("Income:Gains", None),
        )
    )
    with pytest.raises(ReversalError):
        led.reverse(buy.id, day(11), "undo the buy")
