"""M3 golden statements (plan §5): net worth and the category breakdown
are one call each, and every asserted number was computed by hand. The
oracle tests re-derive every section and node total independently from the
Beancount-loaded export, so statements are built with the oracle attached
from the first test."""

import datetime
from decimal import Decimal

import pytest
from scenarios import build_month_of_spending, build_one_paycheck, build_transfer

from ledger.api import Ledger
from ledger.domain.accounts import AccountType
from ledger.query.statements import Section

JAN31 = datetime.date(2024, 1, 31)


class TestMonthOfSpendingStatements:
    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_month_of_spending(ledger)

    def test_balance_sheet_at_month_end(self, led: Ledger):
        sheet = led.balance_sheet(JAN31)
        assert sheet.assets.total.to_dict() == {"USD": Decimal("968.05")}
        # The card was paid off in full: zero balance, pruned entirely.
        assert sheet.liabilities.total.is_empty()
        assert sheet.liabilities.children == ()
        # Equity raw is -2500.00; displayed positive.
        assert sheet.equity.total.to_dict() == {"USD": Decimal("2500.00")}
        assert sheet.net_worth.to_dict() == {"USD": Decimal("968.05")}

    def test_balance_sheet_mid_month(self, led: Ledger):
        sheet = led.balance_sheet(datetime.date(2024, 1, 14))
        # 2500 - 1400 - 4.50 spent from checking so far
        assert sheet.assets.total.to_dict() == {"USD": Decimal("1095.50")}
        # 85.30 + 42.15 charged, nothing paid yet; owed reads positive.
        assert sheet.liabilities.total.to_dict() == {"USD": Decimal("127.45")}
        # Net worth is already final: the later autopay is a transfer.
        assert sheet.net_worth.to_dict() == {"USD": Decimal("968.05")}

    def test_balance_sheet_before_first_transaction(self, led: Ledger):
        sheet = led.balance_sheet(datetime.date(2023, 12, 31))
        for section in sheet.sections:
            assert section.total.is_empty()
        assert sheet.net_worth.is_empty()

    def test_income_statement_category_breakdown(self, led: Ledger):
        stmt = led.income_statement(datetime.date(2024, 1, 1), JAN31)
        assert stmt.income.total.is_empty()
        assert stmt.expenses.total.to_dict() == {"USD": Decimal("1531.95")}
        assert [node.name for node in stmt.expenses.children] == ["Food", "Home"]

        food = stmt.find("Expenses:Food")
        assert food is not None
        assert food.total.to_dict() == {"USD": Decimal("131.95")}
        assert food.own.is_empty()
        assert [(c.name, c.total.to_dict()) for c in food.children] == [
            ("Coffee", {"USD": Decimal("4.50")}),
            ("Groceries", {"USD": Decimal("85.30")}),
            ("Restaurant", {"USD": Decimal("42.15")}),
        ]

        home = stmt.find("Expenses:Home")
        assert home is not None
        # The rent leg was interpolated; it still lands in the breakdown.
        assert home.total.to_dict() == {"USD": Decimal("1400.00")}

        assert stmt.net_income.to_dict() == {"USD": Decimal("-1531.95")}

    def test_income_statement_range_is_inclusive(self, led: Ledger):
        stmt = led.income_statement(
            datetime.date(2024, 1, 12), datetime.date(2024, 1, 14)
        )
        # Restaurant on the 12th + coffee on the 14th, both endpoints in.
        assert stmt.expenses.total.to_dict() == {"USD": Decimal("46.65")}
        assert stmt.net_income.to_dict() == {"USD": Decimal("-46.65")}

    def test_net_worth_identity(self, led: Ledger):
        """net worth == equity (displayed) + net income, per commodity —
        the closing-entry-free counterpart of Assets = Liabilities + Equity."""
        sheet = led.balance_sheet(JAN31)
        stmt = led.income_statement(end=JAN31)
        # 968.05 == 2500.00 + (-1531.95)
        equity = sheet.equity.total.get("USD")
        net_income = stmt.net_income.get("USD")
        net_worth = sheet.net_worth.get("USD")
        assert net_worth is not None and equity is not None and net_income is not None
        assert net_worth.value == equity.value + net_income.value


class TestPaycheckStatements:
    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_one_paycheck(ledger)

    def test_income_statement(self, led: Ledger):
        stmt = led.income_statement()
        # 4432.16 salary + 183.22 bonus; 5 VACHR accrued.
        assert stmt.income.total.to_dict() == {
            "USD": Decimal("4615.38"),
            "VACHR": Decimal("5"),
        }
        # 2018.10 taxes + 45.70 health + 0.98 fees; 1200 IRAUSD cap used.
        assert stmt.expenses.total.to_dict() == {
            "USD": Decimal("2064.78"),
            "IRAUSD": Decimal("1200.00"),
        }
        # Earned minus spent, per commodity, each balancing independently.
        assert stmt.net_income.to_dict() == {
            "USD": Decimal("2550.60"),
            "IRAUSD": Decimal("-1200.00"),
            "VACHR": Decimal("5"),
        }

    def test_category_drill_down(self, led: Ledger):
        stmt = led.income_statement()
        taxes = stmt.find("Expenses:Taxes")
        assert taxes is not None
        assert taxes.total.to_dict() == {
            "USD": Decimal("2018.10"),
            "IRAUSD": Decimal("1200.00"),
        }
        health = stmt.find("Expenses:Health")
        assert health is not None
        assert health.total.to_dict() == {"USD": Decimal("45.70")}
        assert [c.name for c in health.children] == ["Dental", "Insurance", "Vision"]

    def test_balance_sheet(self, led: Ledger):
        sheet = led.balance_sheet()
        # Checking 1350.60 + 401k 961.54 + match 238.46 = 2550.60 USD;
        # the IRS cap is drawn down (negative asset), PTO accrues.
        assert sheet.assets.total.to_dict() == {
            "USD": Decimal("2550.60"),
            "IRAUSD": Decimal("-1200.00"),
            "VACHR": Decimal("5"),
        }
        assert sheet.liabilities.total.is_empty()
        assert sheet.equity.total.is_empty()
        assert sheet.net_worth.to_dict() == sheet.assets.total.to_dict()


class TestTransferStatements:
    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_transfer(ledger)

    def test_transfer_leaves_net_worth_unchanged(self, led: Ledger):
        before = led.balance_sheet(datetime.date(2024, 1, 1))
        after = led.balance_sheet(JAN31)
        assert before.net_worth.to_dict() == {"USD": Decimal("1000.00")}
        assert after.net_worth.to_dict() == {"USD": Decimal("1000.00")}
        assert [(c.name, c.total.to_dict()) for c in after.assets.children] == [
            ("Checking", {"USD": Decimal("750.00")}),
            ("Savings", {"USD": Decimal("250.00")}),
        ]

    def test_income_statement_is_empty(self, led: Ledger):
        stmt = led.income_statement()
        assert stmt.income.total.is_empty()
        assert stmt.expenses.total.is_empty()
        assert stmt.net_income.is_empty()


# --- the oracle -------------------------------------------------------------

_NEGATED = {AccountType.LIABILITY, AccountType.EQUITY, AccountType.INCOME}


def _oracle_totals(
    entries: list,
    prefix: str,
    *,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
) -> dict[str, Decimal]:
    """Per-commodity sums over Beancount's own loaded postings for an
    account prefix — computed from the oracle's data, not ours."""
    from beancount.core import data

    totals: dict[str, Decimal] = {}
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        if start is not None and entry.date < start:
            continue
        if end is not None and entry.date > end:
            continue
        for posting in entry.postings:
            if posting.account == prefix or posting.account.startswith(prefix + ":"):
                currency = posting.units.currency
                totals[currency] = totals.get(currency, Decimal(0)) + (
                    posting.units.number
                )
    return {currency: total for currency, total in totals.items() if total != 0}


def _assert_section_matches(
    section: Section,
    entries: list,
    *,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
) -> None:
    """Every node total in the section tree equals the oracle's prefix sum,
    with the documented display flip applied to the oracle side."""
    negate = section.type in _NEGATED

    def check(prefix: str, to_dict: dict[str, Decimal]) -> None:
        oracle = _oracle_totals(entries, prefix, start=start, end=end)
        if negate:
            oracle = {currency: -total for currency, total in oracle.items()}
        assert to_dict == oracle, prefix

    check(section.title, section.total.to_dict())
    stack = list(section.children)
    while stack:
        node = stack.pop()
        check(node.path, node.total.to_dict())
        stack.extend(node.children)


@pytest.mark.parametrize(
    "build",
    [build_month_of_spending, build_one_paycheck, build_transfer],
    ids=["month", "paycheck", "transfer"],
)
class TestOracleAgreement:
    def test_full_period_statements_match_beancount(
        self, ledger, assert_matches_beancount, build
    ):
        led = build(ledger)
        entries = assert_matches_beancount(led)

        sheet = led.balance_sheet()
        for section in sheet.sections:
            _assert_section_matches(section, entries)
        oracle_net = _oracle_totals(entries, "Assets")
        for currency, total in _oracle_totals(entries, "Liabilities").items():
            oracle_net[currency] = oracle_net.get(currency, Decimal(0)) + total
        oracle_net = {c: t for c, t in oracle_net.items() if t != 0}
        assert sheet.net_worth.to_dict() == oracle_net

        stmt = led.income_statement()
        for section in stmt.sections:
            _assert_section_matches(section, entries)

    def test_ranged_statements_match_beancount(
        self, ledger, assert_matches_beancount, build
    ):
        led = build(ledger)
        entries = assert_matches_beancount(led)
        start, end = datetime.date(2024, 1, 10), datetime.date(2024, 1, 20)

        for section in led.income_statement(start, end).sections:
            _assert_section_matches(section, entries, start=start, end=end)
        for section in led.balance_sheet(end).sections:
            _assert_section_matches(section, entries, end=end)
