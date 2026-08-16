"""M7 prices and market valuation, on both backends (design §9). Every
asserted number is hand-computed."""

import datetime
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.errors import (
    InvalidPriceError,
    MissingPriceError,
    OptionError,
    UnknownCommodityError,
)
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)
JAN1 = datetime.date(2024, 1, 1)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def day(n: int) -> datetime.date:
    return datetime.date(2024, 1, n)


@pytest.fixture
def led(ledger: Ledger) -> Ledger:
    ledger.create_commodity("USD", "currency")
    ledger.create_commodity("AAPL", "security", 0)
    ledger.set_option("operating_currency", "USD")
    ledger.set_option("gains_account_root", "Income:Gains")
    ledger.create_account("Assets:Cash", "asset", JAN1)
    ledger.create_account("Assets:Brokerage", "asset", JAN1, booking_method="FIFO")
    ledger.create_account("Equity:Opening-Balances", "equity", JAN1)
    ledger.record(
        TransactionSpec(
            date=day(1),
            postings=[
                PostingSpec("Assets:Cash", units=A("10000.00")),
                PostingSpec("Equity:Opening-Balances", units=A("-10000.00")),
            ],
        )
    )
    return ledger


def buy(led: Ledger, n: int, units: str, cost: str, cash: str):
    return led.record(
        TransactionSpec(
            date=day(n),
            postings=[
                PostingSpec(
                    "Assets:Brokerage",
                    units=A(units, AAPL),
                    cost=CostSpec(per_unit=Decimal(cost), commodity="USD"),
                ),
                PostingSpec("Assets:Cash", units=A(cash)),
            ],
        )
    )


class TestPriceTable:
    def test_record_and_lookup_most_recent_at_or_before(self, led: Ledger):
        led.record_price("AAPL", day(5), Decimal("100.00"), "USD")
        led.record_price("AAPL", day(10), Decimal("110.00"), "USD")
        assert led.get_price("AAPL", "USD", day(4)) is None
        assert led.get_price("AAPL", "USD", day(5)).price == A("100.00")
        assert led.get_price("AAPL", "USD", day(7)).price == A("100.00")
        assert led.get_price("AAPL", "USD", day(10)).price == A("110.00")
        # None = latest known
        assert led.get_price("AAPL", "USD").price == A("110.00")

    def test_record_price_upserts_its_triple(self, led: Ledger):
        led.record_price("AAPL", day(5), Decimal("100.00"), "USD")
        led.record_price("AAPL", day(5), Decimal("101.00"), "USD")
        [point] = led.list_prices("AAPL", "USD")
        assert point.price == A("101.00")
        assert point.origin == "directive"

    def test_validations(self, led: Ledger):
        with pytest.raises(UnknownCommodityError):
            led.record_price("MSFT", day(5), Decimal("1"), "USD")
        with pytest.raises(UnknownCommodityError):
            led.record_price("AAPL", day(5), Decimal("1"), "EUR")
        with pytest.raises(InvalidPriceError):
            led.record_price("AAPL", day(5), Decimal("0"), "USD")
        with pytest.raises(InvalidPriceError):
            led.record_price("AAPL", day(5), Decimal("-1"), "USD")
        with pytest.raises(InvalidPriceError):
            led.record_price("USD", day(5), Decimal("1"), "USD")
        with pytest.raises(OptionError):
            led.record_price("AAPL", day(5), Decimal("1"), "USD", origin="guess")


class TestImpliedPrices:
    def test_acquisition_cost_is_observed(self, led: Ledger):
        buy(led, 5, "10", "100.00", "-1000.00")
        [point] = led.list_prices("AAPL", "USD")
        assert point.date == day(5)
        assert point.price == A("100.00")
        assert point.origin == "transaction"

    def test_explicit_price_beats_cost_observation(self, led: Ledger):
        led.record(
            TransactionSpec(
                date=day(5),
                postings=[
                    PostingSpec(
                        "Assets:Brokerage",
                        units=A("10", AAPL),
                        cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
                        price=A("101.00"),
                    ),
                    PostingSpec("Assets:Cash", units=A("-1000.00")),
                ],
            )
        )
        [point] = led.list_prices("AAPL", "USD")
        assert point.price == A("101.00")

    def test_sale_price_is_observed_but_lot_cost_is_not(self, led: Ledger):
        buy(led, 5, "10", "100.00", "-1000.00")
        led.record(
            TransactionSpec(
                date=day(10),
                postings=[
                    PostingSpec(
                        "Assets:Brokerage",
                        units=A("-4", AAPL),
                        cost=CostSpec(),
                        price=A("130.00"),
                    ),
                    PostingSpec("Assets:Cash", units=A("520.00")),
                    PostingSpec("Income:Gains", units=None),
                ],
            )
        )
        by_date = {p.date: p for p in led.list_prices("AAPL", "USD")}
        assert by_date[day(5)].price == A("100.00")
        assert by_date[day(10)].price == A("130.00")
        assert len(by_date) == 2

    def test_observation_never_overwrites_a_directive(self, led: Ledger):
        led.record_price("AAPL", day(5), Decimal("99.50"), "USD")
        buy(led, 5, "10", "100.00", "-1000.00")
        [point] = led.list_prices("AAPL", "USD")
        assert point.price == A("99.50")
        assert point.origin == "directive"

    def test_directive_replaces_an_observation(self, led: Ledger):
        buy(led, 5, "10", "100.00", "-1000.00")
        led.record_price("AAPL", day(5), Decimal("99.50"), "USD")
        [point] = led.list_prices("AAPL", "USD")
        assert point.price == A("99.50")
        assert point.origin == "directive"

    def test_transaction_prices_are_not_exported(self, led: Ledger):
        buy(led, 5, "10", "100.00", "-1000.00")
        led.record_price("AAPL", day(7), Decimal("105.00"), "USD")
        text = led.export_beancount_string()
        assert "2024-01-07 price AAPL 105.00 USD" in text
        assert "2024-01-05 price" not in text


class TestValuation:
    def holdings(self, led: Ledger):
        """10 AAPL at 100.00 plus 5 at 120.00, and 8400.00 cash left."""
        buy(led, 5, "10", "100.00", "-1000.00")
        buy(led, 6, "5", "120.00", "-600.00")

    def test_market_value_uses_most_recent_price(self, led: Ledger):
        self.holdings(led)
        led.record_price("AAPL", day(15), Decimal("140.00"), "USD")
        # 15 x 140.00 = 2100.00
        assert led.market_value("Assets:Brokerage").to_decimal() == Decimal("2100.00")
        # at day 6 the last price is the observed cost 120.00: 15 x 120
        assert led.market_value("Assets:Brokerage", day(6)).to_decimal() == (
            Decimal("1800.00")
        )

    def test_market_value_counts_the_target_commodity_at_face(self, led: Ledger):
        self.holdings(led)
        led.record_price("AAPL", day(15), Decimal("140.00"), "USD")
        # cash 8400.00 + stock 2100.00
        assert led.market_value("Assets").to_decimal() == Decimal("10500.00")

    def test_unrealized_gain(self, led: Ledger):
        self.holdings(led)
        led.record_price("AAPL", day(15), Decimal("140.00"), "USD")
        # 10 x (140-100) + 5 x (140-120) = 400 + 100 = 500
        assert led.unrealized_gain("Assets:Brokerage").to_decimal() == Decimal("500.00")
        # at day 5 only the first lot exists, price = observed cost 100.00
        assert led.unrealized_gain("Assets:Brokerage", day(5)).to_decimal() == (
            Decimal("0.00")
        )

    def test_unrealized_gain_reflects_reductions(self, led: Ledger):
        self.holdings(led)
        led.record(
            TransactionSpec(
                date=day(10),
                postings=[
                    PostingSpec(
                        "Assets:Brokerage",
                        units=A("-12", AAPL),
                        cost=CostSpec(),
                        price=A("130.00"),
                    ),
                    PostingSpec("Assets:Cash", units=A("1560.00")),
                    PostingSpec("Income:Gains", units=None),
                ],
            )
        )
        led.record_price("AAPL", day(15), Decimal("140.00"), "USD")
        # 3 remaining from the 120.00 lot: 3 x (140-120) = 60
        assert led.unrealized_gain("Assets:Brokerage").to_decimal() == Decimal("60.00")

    def test_missing_price_raises(self, led: Ledger):
        vachr = Commodity("VACHR", CommodityKind.TRACKING, 0)
        led.create_commodity("VACHR", "tracking", 0)
        led.create_account("Assets:Vacation", "asset", JAN1)
        led.create_account("Income:Vacation", "income", JAN1)
        led.record(
            TransactionSpec(
                date=day(5),
                postings=[
                    PostingSpec("Assets:Vacation", units=A("5", vachr)),
                    PostingSpec("Income:Vacation", units=A("-5", vachr)),
                ],
            )
        )
        with pytest.raises(MissingPriceError):
            led.market_value("Assets:Vacation")

    def test_valuation_commodity_defaults_to_operating_currency(self, ledger: Ledger):
        ledger.create_commodity("USD", "currency")
        ledger.create_account("Assets:Cash", "asset", JAN1)
        ledger.create_account("Equity:Opening-Balances", "equity", JAN1)
        ledger.record(
            TransactionSpec(
                date=day(1),
                postings=[
                    PostingSpec("Assets:Cash", units=A("100.00")),
                    PostingSpec("Equity:Opening-Balances", units=A("-100.00")),
                ],
            )
        )
        with pytest.raises(OptionError):
            ledger.market_value("Assets:Cash")
        ledger.set_option("operating_currency", "USD")
        assert ledger.market_value("Assets:Cash").to_decimal() == Decimal("100.00")
