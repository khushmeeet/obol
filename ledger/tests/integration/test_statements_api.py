"""Statement queries against both backends (plan §5): section structure,
the display sign convention (flipped here and nowhere else), tree rollups,
pruning of zeroed accounts, and inclusive date bounds."""

import datetime
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.accounts import AccountType
from ledger.domain.amount import Amount, Commodity, CommodityKind
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
        ("Assets:Bank:A", "asset"),
        ("Assets:Bank:B", "asset"),
        ("Liabilities:Card", "liability"),
        ("Equity:Opening-Balances", "equity"),
        ("Income:Salary", "income"),
        ("Expenses:Food", "expense"),
        ("Expenses:Food:Coffee", "expense"),
    ]:
        ledger.create_account(path, type_, JAN)
    return ledger


class TestEmptyLedger:
    def test_balance_sheet_shape(self, led: Ledger):
        sheet = led.balance_sheet()
        assert [s.title for s in sheet.sections] == ["Assets", "Liabilities", "Equity"]
        assert [s.type for s in sheet.sections] == [
            AccountType.ASSET,
            AccountType.LIABILITY,
            AccountType.EQUITY,
        ]
        for section in sheet.sections:
            assert section.total.is_empty()
            assert section.children == ()
        assert sheet.net_worth.is_empty()

    def test_income_statement_shape(self, led: Ledger):
        stmt = led.income_statement()
        assert [s.title for s in stmt.sections] == ["Income", "Expenses"]
        for section in stmt.sections:
            assert section.total.is_empty()
            assert section.children == ()
        assert stmt.net_income.is_empty()

    def test_missing_section_raises(self, led: Ledger):
        with pytest.raises(KeyError):
            led.balance_sheet().section(AccountType.INCOME)
        with pytest.raises(KeyError):
            led.income_statement().section(AccountType.ASSET)


class TestDisplaySigns:
    """Raw amounts are signed (design §4); the statement layer negates
    Liabilities, Equity and Income for display, and only those."""

    def test_liability_reads_positive(self, led: Ledger):
        led.record(
            txn(5, leg("Liabilities:Card", "-50.00"), leg("Expenses:Food", "50.00"))
        )
        sheet = led.balance_sheet()
        assert sheet.liabilities.total.to_dict() == {"USD": Decimal("50.00")}
        assert sheet.net_worth.to_dict() == {"USD": Decimal("-50.00")}

    def test_equity_and_income_read_positive(self, led: Ledger):
        led.record(
            txn(
                1,
                leg("Equity:Opening-Balances", "-300.00"),
                leg("Assets:Checking", "300.00"),
            )
        )
        led.record(
            txn(3, leg("Income:Salary", "-100.00"), leg("Assets:Checking", "100.00"))
        )
        sheet = led.balance_sheet()
        stmt = led.income_statement()
        assert sheet.equity.total.to_dict() == {"USD": Decimal("300.00")}
        assert stmt.income.total.to_dict() == {"USD": Decimal("100.00")}
        assert stmt.net_income.to_dict() == {"USD": Decimal("100.00")}

    def test_assets_and_expenses_stay_raw(self, led: Ledger):
        led.record(
            txn(2, leg("Assets:Checking", "-20.00"), leg("Expenses:Food", "20.00"))
        )
        assert led.balance_sheet().assets.total.to_dict() == {"USD": Decimal("-20.00")}
        stmt = led.income_statement()
        assert stmt.expenses.total.to_dict() == {"USD": Decimal("20.00")}
        assert stmt.net_income.to_dict() == {"USD": Decimal("-20.00")}


class TestTree:
    def test_own_vs_total_when_parent_also_posts(self, led: Ledger):
        led.record(
            txn(2, leg("Assets:Checking", "-10.00"), leg("Expenses:Food", "10.00"))
        )
        led.record(
            txn(
                3,
                leg("Assets:Checking", "-4.50"),
                leg("Expenses:Food:Coffee", "4.50"),
            )
        )
        food = led.income_statement().find("Expenses:Food")
        assert food is not None
        assert food.own.to_dict() == {"USD": Decimal("10.00")}
        assert food.total.to_dict() == {"USD": Decimal("14.50")}
        assert [child.name for child in food.children] == ["Coffee"]
        assert food.children[0].own.to_dict() == {"USD": Decimal("4.50")}

    def test_intermediate_segment_becomes_node(self, led: Ledger):
        led.record(txn(2, leg("Assets:Bank:A", "-5.00"), leg("Expenses:Food", "5.00")))
        bank = led.balance_sheet().find("Assets:Bank")
        assert bank is not None
        assert bank.own.is_empty()  # never posted to directly
        assert bank.total.to_dict() == {"USD": Decimal("-5.00")}
        assert led.balance_sheet().find("Assets:Bank:A") is not None

    def test_offsetting_children_keep_parent_with_empty_total(self, led: Ledger):
        led.record(txn(2, leg("Assets:Bank:A", "100.00"), leg("Assets:Bank:B", None)))
        sheet = led.balance_sheet()
        bank = sheet.find("Assets:Bank")
        assert bank is not None
        assert bank.total.is_empty()
        assert [child.name for child in bank.children] == ["A", "B"]
        assert sheet.assets.total.is_empty()

    def test_zeroed_account_is_pruned(self, led: Ledger):
        led.record(
            txn(5, leg("Liabilities:Card", "-50.00"), leg("Expenses:Food", "50.00"))
        )
        led.record(
            txn(20, leg("Assets:Checking", "-50.00"), leg("Liabilities:Card", "50.00"))
        )
        sheet = led.balance_sheet()
        assert sheet.find("Liabilities:Card") is None
        assert sheet.liabilities.children == ()
        assert sheet.liabilities.total.is_empty()

    def test_find_misses(self, led: Ledger):
        led.record(
            txn(2, leg("Assets:Checking", "-20.00"), leg("Expenses:Food", "20.00"))
        )
        stmt = led.income_statement()
        assert stmt.find("Expenses:Home") is None
        assert stmt.find("Assets:Checking") is None  # wrong statement
        assert stmt.find("Expenses") is None  # the root is a section, not a node


class TestDateBounds:
    @pytest.fixture
    def spent(self, led: Ledger) -> Ledger:
        for day in (5, 10, 15):
            led.record(
                txn(
                    day,
                    leg("Assets:Checking", "-10.00"),
                    leg("Expenses:Food", "10.00"),
                )
            )
        return led

    def test_income_statement_bounds_are_inclusive(self, spent: Ledger):
        stmt = spent.income_statement(
            datetime.date(2024, 1, 5), datetime.date(2024, 1, 10)
        )
        assert stmt.expenses.total.to_dict() == {"USD": Decimal("20.00")}

    def test_income_statement_open_ended_sides(self, spent: Ledger):
        assert spent.income_statement(
            start=datetime.date(2024, 1, 10)
        ).expenses.total.to_dict() == {"USD": Decimal("20.00")}
        assert spent.income_statement(
            end=datetime.date(2024, 1, 10)
        ).expenses.total.to_dict() == {"USD": Decimal("20.00")}

    def test_balance_sheet_includes_the_day(self, spent: Ledger):
        on = datetime.date(2024, 1, 10)
        assert spent.balance_sheet(on).assets.total.to_dict() == {
            "USD": Decimal("-20.00")
        }
        assert spent.balance_sheet(datetime.date(2024, 1, 4)).assets.total.is_empty()
