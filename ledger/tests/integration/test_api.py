"""Ledger API against both backends (the `ledger` fixture is parametrized
over in-memory and SQLite)."""

import datetime
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.errors import (
    AccountNotOpenError,
    CommodityMismatchError,
    CommodityNotAllowedError,
    DuplicateAccountError,
    DuplicateCommodityError,
    DuplicateSourceError,
    InvalidTransactionError,
    OptionError,
    UnbalancedTransactionError,
    UnknownAccountError,
    UnknownCommodityError,
)
from ledger.domain.transaction import PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
JAN = datetime.date(2024, 1, 1)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def leg(account: str, text: str | None, commodity: Commodity = USD) -> PostingSpec:
    units = A(text, commodity) if text is not None else None
    return PostingSpec(account=account, units=units)


def txn(day: int, *postings: PostingSpec, **kwargs) -> TransactionSpec:
    return TransactionSpec(
        date=datetime.date(2024, 1, day), postings=list(postings), **kwargs
    )


@pytest.fixture
def led(ledger: Ledger) -> Ledger:
    ledger.create_commodity("USD", "currency")
    for path, type_ in [
        ("Assets:Checking", "asset"),
        ("Assets:Savings", "asset"),
        ("Expenses:Food", "expense"),
        ("Expenses:Rent", "expense"),
        ("Income:Salary", "income"),
    ]:
        ledger.create_account(path, type_, JAN)
    return ledger


class TestSetup:
    def test_create_commodity_normalizes_kind(self, ledger: Ledger):
        commodity = ledger.create_commodity("EUR", "CURRENCY")
        assert commodity.kind is CommodityKind.CURRENCY
        assert ledger.get_commodity("EUR") == commodity

    def test_duplicate_commodity_rejected(self, led: Ledger):
        with pytest.raises(DuplicateCommodityError):
            led.create_commodity("USD", "currency")

    def test_duplicate_account_rejected(self, led: Ledger):
        with pytest.raises(DuplicateAccountError):
            led.create_account("Assets:Checking", "asset", JAN)

    def test_get_account_round_trip(self, led: Ledger):
        account = led.get_account("Assets:Checking")
        assert account is not None
        assert account.opened_on == JAN
        assert account.closed_on is None

    def test_options_round_trip(self, led: Ledger):
        led.set_option("operating_currency", "USD")
        assert led.get_option("operating_currency") == "USD"
        assert led.get_option("gains_account_root") is None

    def test_unknown_option_key_rejected(self, led: Ledger):
        with pytest.raises(OptionError):
            led.set_option("operating_curency", "USD")

    def test_bad_multiplier_value_rejected(self, led: Ledger):
        with pytest.raises(OptionError):
            led.set_option("inferred_tolerance_multiplier", "loose")


class TestRecord:
    def test_record_and_read_back(self, led: Ledger):
        recorded = led.record(
            txn(
                10,
                leg("Assets:Checking", "-42.50"),
                leg("Expenses:Food", "42.50"),
                payee="Corner Shop",
                narration="groceries",
                metadata={"note": "weekly run"},
            )
        )
        assert recorded.id is not None
        fetched = led.get_transaction(recorded.id)
        assert fetched is not None
        assert fetched.payee == "Corner Shop"
        assert fetched.narration == "groceries"
        assert fetched.date == datetime.date(2024, 1, 10)
        assert fetched.metadata == {"note": "weekly run"}
        assert [p.units for p in fetched.postings] == [A("-42.50"), A("42.50")]
        assert fetched.created_at is not None

    def test_posting_order_preserved(self, led: Ledger):
        legs = [
            leg("Income:Salary", "-100.00"),
            leg("Assets:Savings", "25.00"),
            leg("Assets:Checking", "50.00"),
            leg("Expenses:Food", "25.00"),
        ]
        recorded = led.record(txn(5, *legs))
        fetched = led.get_transaction(recorded.id)
        assert [p.account for p in fetched.postings] == [
            "Income:Salary",
            "Assets:Savings",
            "Assets:Checking",
            "Expenses:Food",
        ]

    def test_interpolated_flag_persisted(self, led: Ledger):
        recorded = led.record(
            txn(5, leg("Assets:Checking", "-10.00"), leg("Expenses:Food", None))
        )
        fetched = led.get_transaction(recorded.id)
        assert [p.interpolated for p in fetched.postings] == [False, True]
        assert fetched.postings[1].units == A("10.00")

    def test_unknown_account_rejected(self, led: Ledger):
        with pytest.raises(UnknownAccountError):
            led.record(
                txn(5, leg("Assets:Nope", "-1.00"), leg("Expenses:Food", "1.00"))
            )

    def test_account_not_yet_open_rejected(self, led: Ledger):
        led.create_account("Assets:Brokerage", "asset", datetime.date(2024, 6, 1))
        with pytest.raises(AccountNotOpenError):
            led.record(
                txn(5, leg("Assets:Brokerage", "-1.00"), leg("Expenses:Food", "1.00"))
            )

    def test_closed_account_rejected(self, led: Ledger):
        led.close_account("Assets:Savings", datetime.date(2024, 1, 10))
        with pytest.raises(AccountNotOpenError):
            led.record(
                txn(15, leg("Assets:Savings", "-1.00"), leg("Expenses:Food", "1.00"))
            )
        # on the close date itself, still allowed (inclusive)
        led.record(
            txn(10, leg("Assets:Savings", "-1.00"), leg("Expenses:Food", "1.00"))
        )

    def test_unregistered_commodity_rejected(self, led: Ledger):
        eur = Commodity("EUR", CommodityKind.CURRENCY, 2)
        with pytest.raises(UnknownCommodityError):
            led.record(
                txn(
                    5,
                    leg("Assets:Checking", "-1.00", eur),
                    leg("Expenses:Food", "1.00", eur),
                )
            )

    def test_commodity_definition_must_match_registered(self, led: Ledger):
        impostor = Commodity("USD", CommodityKind.SECURITY, 2)
        with pytest.raises(CommodityMismatchError):
            led.record(
                txn(
                    5,
                    leg("Assets:Checking", "-1.00", impostor),
                    leg("Expenses:Food", "1.00", impostor),
                )
            )

    def test_allowed_commodities_enforced(self, led: Ledger):
        led.create_commodity("EUR", "currency")
        eur = led.get_commodity("EUR")
        led.create_account("Assets:UsdOnly", "asset", JAN, allowed_commodities=["USD"])
        with pytest.raises(CommodityNotAllowedError):
            led.record(
                txn(
                    5,
                    leg("Assets:UsdOnly", "-1.00", eur),
                    leg("Expenses:Food", "1.00", eur),
                )
            )

    def test_allowed_commodities_checked_on_interpolated_leg(self, led: Ledger):
        led.create_commodity("EUR", "currency")
        eur = led.get_commodity("EUR")
        led.create_account(
            "Expenses:UsdOnly", "expense", JAN, allowed_commodities=["USD"]
        )
        with pytest.raises(CommodityNotAllowedError):
            led.record(
                txn(
                    5,
                    leg("Assets:Checking", "-1.00", eur),
                    leg("Expenses:UsdOnly", None),
                )
            )

    def test_single_posting_rejected(self, led: Ledger):
        with pytest.raises(InvalidTransactionError):
            led.record(txn(5, leg("Assets:Checking", "-1.00")))

    def test_duplicate_source_ref_rejected(self, led: Ledger):
        make = lambda: txn(  # noqa: E731
            5,
            leg("Assets:Checking", "-1.00"),
            leg("Expenses:Food", "1.00"),
            source="plaid",
            source_ref="txn_abc123",
        )
        led.record(make())
        with pytest.raises(DuplicateSourceError):
            led.record(make())
        # same ref under a different source is a different transaction
        other = make()
        other.source = "import"
        led.record(other)

    def test_rejected_transaction_leaves_no_trace(self, led: Ledger):
        with pytest.raises(UnbalancedTransactionError):
            led.record(
                txn(5, leg("Assets:Checking", "-1.00"), leg("Expenses:Food", "2.00"))
            )
        assert led.balance("Assets:Checking").is_empty()
        assert led.journal("Assets:Checking") == []

    def test_tolerance_multiplier_option_applies(self, led: Ledger):
        led.set_option("inferred_tolerance_multiplier", "2")
        led.record(
            txn(5, leg("Assets:Checking", "-10.00"), leg("Expenses:Food", "10.008"))
        )


class TestBalanceAndJournal:
    @pytest.fixture
    def recorded(self, led: Ledger) -> Ledger:
        led.record(
            txn(5, leg("Income:Salary", "-1000.00"), leg("Assets:Checking", "1000.00"))
        )
        led.record(
            txn(10, leg("Assets:Checking", "-42.50"), leg("Expenses:Food", "42.50"))
        )
        led.record(
            txn(20, leg("Assets:Checking", "-800.00"), leg("Expenses:Rent", "800.00"))
        )
        return led

    def test_balance(self, recorded: Ledger):
        assert recorded.balance("Assets:Checking").to_dict() == {
            "USD": Decimal("157.50")
        }

    def test_balance_at_date_is_inclusive(self, recorded: Ledger):
        on = datetime.date(2024, 1, 10)
        assert recorded.balance("Assets:Checking", on).to_dict() == {
            "USD": Decimal("957.50")
        }

    def test_balance_before_any_postings_is_empty(self, recorded: Ledger):
        assert recorded.balance("Assets:Checking", datetime.date(2024, 1, 2)).is_empty()

    def test_rollup_over_children_without_parent_row(self, recorded: Ledger):
        # "Expenses" itself was never opened; prefix rollup still works.
        assert recorded.balance("Expenses").to_dict() == {"USD": Decimal("842.50")}

    def test_exclude_children(self, recorded: Ledger):
        assert recorded.balance("Expenses", include_children=False).is_empty()

    def test_no_prefix_trap(self, led: Ledger):
        led.create_account("Assets:Card", "asset", JAN)
        led.create_account("Assets:CardOld", "asset", JAN)
        led.record(txn(5, leg("Assets:Card", "-5.00"), leg("Assets:CardOld", "5.00")))
        assert led.balance("Assets:Card").to_dict() == {"USD": Decimal("-5.00")}

    def test_whole_ledger_sums_to_zero(self, recorded: Ledger):
        totals = 0
        for root in ("Assets", "Liabilities", "Equity", "Income", "Expenses"):
            amount = recorded.balance(root).get("USD")
            if amount is not None:
                totals += amount.value
        assert totals == 0

    def test_journal_order_and_content(self, recorded: Ledger):
        entries = recorded.journal("Assets:Checking")
        assert [e.date.day for e in entries] == [5, 10, 20]
        assert [e.posting.units for e in entries] == [
            A("1000.00"),
            A("-42.50"),
            A("-800.00"),
        ]

    def test_journal_date_range_inclusive(self, recorded: Ledger):
        entries = recorded.journal(
            "Assets:Checking",
            datetime.date(2024, 1, 10),
            datetime.date(2024, 1, 20),
        )
        assert [e.date.day for e in entries] == [10, 20]

    def test_journal_includes_children(self, recorded: Ledger):
        entries = recorded.journal("Expenses")
        assert [e.posting.account for e in entries] == [
            "Expenses:Food",
            "Expenses:Rent",
        ]


class TestSQLitePersistence:
    def test_reopen_preserves_everything(self, tmp_path):
        path = tmp_path / "ledger.db"
        with Ledger.open(path) as led:
            led.create_commodity("USD", "currency")
            led.create_account("Assets:Checking", "asset", JAN)
            led.create_account("Expenses:Food", "expense", JAN)
            recorded = led.record(
                txn(10, leg("Assets:Checking", "-42.50"), leg("Expenses:Food", None))
            )
            transaction_id = recorded.id

        with Ledger.open(path) as led:  # migrate() must be idempotent
            assert led.balance("Assets:Checking").to_dict() == {
                "USD": Decimal("-42.50")
            }
            fetched = led.get_transaction(transaction_id)
            assert fetched is not None
            assert fetched.postings[1].interpolated

    def test_borrowed_connection_constructor(self, tmp_path):
        from ledger.storage.db import connect

        conn = connect(tmp_path / "ledger.db")
        led = Ledger(conn)
        led.create_commodity("USD", "currency")
        assert led.get_commodity("USD") is not None
        conn.close()

    def test_constructor_requires_exactly_one_source(self):
        with pytest.raises(ValueError):
            Ledger()
