"""Golden scenarios (design §14): hand-written, hand-verified, kept as
documentation. Every asserted number was computed by hand. The scenario
builders live in scenarios.py, shared with the M2 export tests."""

import datetime
from decimal import Decimal

import pytest
from scenarios import (
    build_midlife_connection,
    build_month_of_spending,
    build_paycheck_accounts,
    build_stock_sale,
    build_transfer,
    paycheck_spec,
)

from ledger.api import Ledger
from ledger.domain.directives import AssertionStatus


class TestMonthOfOrdinarySpending:
    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_month_of_spending(ledger)

    def test_checking_balance(self, led: Ledger):
        # 2500.00 - 1400.00 - 4.50 - 127.45
        assert led.balance("Assets:Checking").to_dict() == {"USD": Decimal("968.05")}

    def test_card_paid_off_exactly(self, led: Ledger):
        # -85.30 - 42.15 + 127.45 = 0 -> empty inventory
        assert led.balance("Liabilities:Card").is_empty()

    def test_category_rollups(self, led: Ledger):
        assert led.balance("Expenses:Food").to_dict() == {"USD": Decimal("131.95")}
        assert led.balance("Expenses").to_dict() == {"USD": Decimal("1531.95")}

    def test_mid_month_snapshot(self, led: Ledger):
        on = datetime.date(2024, 1, 14)
        assert led.balance("Assets:Checking", on).to_dict() == {
            "USD": Decimal("1095.50")  # 2500 - 1400 - 4.50
        }
        assert led.balance("Liabilities:Card", on).to_dict() == {
            "USD": Decimal("-127.45")
        }

    def test_nothing_invented_or_destroyed(self, led: Ledger):
        total = 0
        for root in ("Assets", "Liabilities", "Equity", "Income", "Expenses"):
            amount = led.balance(root).get("USD")
            if amount is not None:
                total += amount.value
        assert total == 0

    def test_card_journal(self, led: Ledger):
        entries = led.journal("Liabilities:Card")
        assert [(e.date.day, str(e.posting.units.to_decimal())) for e in entries] == [
            (5, "-85.30"),
            (12, "-42.15"),
            (28, "127.45"),
        ]


class TestPaycheck:
    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_paycheck_accounts(ledger)

    def test_one_paycheck(self, led: Ledger):
        recorded = led.record(paycheck_spec(15))
        assert len(recorded.postings) == 18
        assert led.balance("Assets:BofA:Checking").to_dict() == {
            "USD": Decimal("1350.60")
        }
        assert led.balance("Assets:Hooli:Vacation").to_dict() == {"VACHR": Decimal("5")}
        assert led.balance("Assets:Federal:PreTax401k").to_dict() == {
            "IRAUSD": Decimal("-1200.00")
        }
        assert led.balance("Expenses:Taxes").to_dict() == {
            "USD": Decimal("2018.10"),
            "IRAUSD": Decimal("1200.00"),
        }

    def test_two_pay_periods_accumulate(self, led: Ledger):
        led.record(paycheck_spec(15))
        led.record(paycheck_spec(31))
        assert led.balance("Assets:BofA:Checking").to_dict() == {
            "USD": Decimal("2701.20")
        }
        assert led.balance("Assets:Hooli:Vacation").to_dict() == {
            "VACHR": Decimal("10")
        }
        assert led.balance("Income").to_dict() == {
            "USD": Decimal("-9230.76"),
            "VACHR": Decimal("-10"),
        }

    def test_shape_preserved_for_faithful_export(self, led: Ledger):
        """Posting order (seq) reproduces the paycheck in its original
        shape — a design requirement for M2 export."""
        recorded = led.record(paycheck_spec(15))
        fetched = led.get_transaction(recorded.id)
        assert [p.account for p in fetched.postings] == [
            p.account for p in paycheck_spec(15).postings
        ]
        assert fetched.postings[2].interpolated
        assert str(fetched.postings[2].units.to_decimal()) == "1350.60"


class TestMidlifeConnection:
    """The design §8 scenario: an account that existed before Obol did,
    padded against its first Plaid-reported balance so every subsequent
    number is correct without fabricating history."""

    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_midlife_connection(ledger)

    def test_both_assertions_pass(self, led: Ledger):
        first, second = led.list_assertions()
        assert first.status is AssertionStatus.PASS
        assert second.status is AssertionStatus.PASS
        assert first.difference.value == 0
        assert second.difference.value == 0

    def test_pad_booked_the_pre_history(self, led: Ledger):
        # 5000.00 reported - (-54.23 + 2000.00 - 120.00) recorded = 3174.23
        pad = led.list_pads()[0]
        assert pad.generated_txn_id is not None
        generated = led.get_transaction(pad.generated_txn_id)
        assert generated.date == datetime.date(2024, 3, 1)
        assert {p.account: str(p.units.to_decimal()) for p in generated.postings} == {
            "Assets:Chase:Checking": "3174.23",
            "Equity:Opening-Balances": "-3174.23",
        }

    def test_final_balances(self, led: Ledger):
        assert led.balance("Assets:Chase:Checking").to_dict() == {
            "USD": Decimal("4939.50")
        }
        assert led.balance("Equity:Opening-Balances").to_dict() == {
            "USD": Decimal("-3174.23")
        }
        assert led.balance("Expenses:Food:Groceries").to_dict() == {
            "USD": Decimal("114.73")
        }
        assert led.balance("Expenses:Utilities").to_dict() == {"USD": Decimal("120.00")}
        assert led.balance("Income").to_dict() == {"USD": Decimal("-2000.00")}

    def test_second_assertion_needed_no_pad(self, led: Ledger):
        pads = led.list_pads()
        assert len(pads) == 1
        assert pads[0].consumed_by == led.list_assertions()[0].id

    def test_validates_clean(self, led: Ledger):
        report = led.validate()
        assert report.ok, str(report)


class TestStockSale:
    """The M7 exit criterion (design §15): buy, partially sell under
    FIFO, and produce cost basis and realized gain — every number in the
    builder's docstring computed by hand."""

    @pytest.fixture
    def led(self, ledger: Ledger) -> Ledger:
        return build_stock_sale(ledger)

    def test_final_balances(self, led: Ledger):
        # cash: 10000 - 1250 - 1204 + 1882.05
        assert led.balance("Assets:ETrade:Cash").to_dict() == {
            "USD": Decimal("9428.05")
        }
        assert led.balance("Assets:ETrade:AAPL").to_dict() == {"AAPL": Decimal("5")}
        assert led.balance("Income:Gains").to_dict() == {"USD": Decimal("-297.00")}
        assert led.balance("Expenses").to_dict() == {"USD": Decimal("8.95")}

    def test_fifo_took_the_older_lot_first(self, led: Ledger):
        sale = led.list_transactions()[-1]
        stock = sale.postings[0]
        lots = led.list_lots("Assets:ETrade:AAPL")
        assert [
            (match.lot_id, Decimal(match.quantity).scaleb(-8).normalize())
            for match in stock.lot_matches
        ] == [(lots[0].id, Decimal("8")), (lots[1].id, Decimal("2"))]
        # cost basis 8 x 156.25 + 2 x 172.00 = 1594.00
        assert stock.weight.to_decimal() == Decimal("-1594.00")

    def test_realized_gain_was_interpolated(self, led: Ledger):
        sale = led.list_transactions()[-1]
        gains = sale.postings[-1]
        assert gains.account == "Income:Gains:ETrade"
        assert gains.interpolated
        assert gains.units.to_decimal() == Decimal("-297.00")

    def test_remaining_inventory(self, led: Ledger):
        [position] = led.inventory("Assets:ETrade:AAPL").positions()
        assert position.units.to_decimal() == Decimal("5")
        assert position.cost is not None
        assert position.cost.per_unit.to_decimal() == Decimal("172.00")
        assert position.cost.date == datetime.date(2024, 2, 9)

    def test_market_value_and_unrealized_gain(self, led: Ledger):
        on = datetime.date(2024, 3, 28)
        # 5 x 190.50
        assert led.market_value("Assets:ETrade:AAPL", on).to_decimal() == (
            Decimal("952.50")
        )
        # 5 x (190.50 - 172.00)
        assert led.unrealized_gain("Assets:ETrade:AAPL", on).to_decimal() == (
            Decimal("92.50")
        )

    def test_income_statement_shows_the_gain(self, led: Ledger):
        statement = led.income_statement()
        assert statement.net_income.to_dict() == {"USD": Decimal("288.05")}
        gains = statement.find("Income:Gains:ETrade")
        assert gains is not None
        assert gains.total.to_dict() == {"USD": Decimal("297.00")}

    def test_validates_clean(self, led: Ledger):
        report = led.validate()
        assert report.ok, str(report)


class TestTransferBetweenOwnAccounts:
    def test_transfer_moves_value_without_changing_net_worth(self, ledger: Ledger):
        led = build_transfer(ledger)
        assert led.balance("Assets:Checking").to_dict() == {"USD": Decimal("750.00")}
        assert led.balance("Assets:Savings").to_dict() == {"USD": Decimal("250.00")}
        assert led.balance("Assets").to_dict() == {"USD": Decimal("1000.00")}
