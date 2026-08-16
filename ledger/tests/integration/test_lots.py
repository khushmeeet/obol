"""M7 lots, booking, and gains through the public API, on both backends
(design §7). Every asserted number is hand-computed."""

import datetime
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.booking import LotMatch
from ledger.domain.errors import (
    AmbiguousLotError,
    InsufficientLotError,
    NoLotMatchError,
    NotSupportedError,
    ReversalError,
    UnknownAccountError,
    UnknownCommodityError,
)
from ledger.domain.inventory import Cost, Position
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)
MSFT = Commodity("MSFT", CommodityKind.SECURITY, 0)
JAN1 = datetime.date(2024, 1, 1)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def day(n: int) -> datetime.date:
    return datetime.date(2024, 1, n)


def leg(account: str, text: str | None, commodity: Commodity = USD) -> PostingSpec:
    units = A(text, commodity) if text is not None else None
    return PostingSpec(account=account, units=units)


def stock_leg(
    account: str,
    units: str,
    *,
    cost: CostSpec | None = None,
    price: str | None = None,
    commodity: Commodity = AAPL,
) -> PostingSpec:
    return PostingSpec(
        account=account,
        units=A(units, commodity),
        cost=cost,
        price=A(price) if price is not None else None,
    )


def at_cost(cost: str, *, date: datetime.date | None = None, label=None) -> CostSpec:
    return CostSpec(per_unit=Decimal(cost), commodity="USD", date=date, label=label)


@pytest.fixture
def led(ledger: Ledger) -> Ledger:
    ledger.create_commodity("USD", "currency")
    ledger.create_commodity("AAPL", "security", 0)
    ledger.set_option("gains_account_root", "Income:Gains")
    ledger.create_account("Assets:Cash", "asset", JAN1)
    ledger.create_account("Assets:Brokerage", "asset", JAN1)  # STRICT
    ledger.create_account("Assets:Fifo", "asset", JAN1, booking_method="FIFO")
    ledger.create_account("Equity:Opening-Balances", "equity", JAN1)
    ledger.record(
        TransactionSpec(
            date=day(1),
            postings=[
                leg("Assets:Cash", "10000.00"),
                leg("Equity:Opening-Balances", "-10000.00"),
            ],
            narration="opening",
        )
    )
    return ledger


def txn(led: Ledger, n: int, *postings: PostingSpec, **kwargs):
    return led.record(TransactionSpec(date=day(n), postings=list(postings), **kwargs))


def buy(led: Ledger, n: int, account, units, cost, cash, **kwargs):
    return txn(
        led,
        n,
        stock_leg(account, units, cost=at_cost(cost)),
        leg("Assets:Cash", cash),
        **kwargs,
    )


class TestAcquisition:
    def test_buy_creates_a_lot(self, led: Ledger):
        buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        [lot] = led.list_lots("Assets:Brokerage")
        assert lot.account == "Assets:Brokerage"
        assert lot.commodity == AAPL
        assert lot.acquired_on == day(5)  # defaulted to the transaction date
        assert lot.recorded_on == day(5)
        assert lot.original_quantity == A("10", AAPL).value
        assert lot.cost == A("100.00")
        assert lot.label is None

    def test_acquisition_weight_balances_against_cash(self, led: Ledger):
        recorded = buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        stock = recorded.postings[0]
        assert stock.weight == A("1000.00")
        assert stock.cost == Cost(per_unit=A("100.00"), date=day(5))

    def test_explicit_lot_date(self, led: Ledger):
        txn(
            led,
            5,
            stock_leg(
                "Assets:Brokerage",
                "10",
                cost=at_cost("100.00", date=datetime.date(2023, 6, 1)),
            ),
            leg("Assets:Cash", "-1000.00"),
        )
        [lot] = led.list_lots("Assets:Brokerage")
        assert lot.acquired_on == datetime.date(2023, 6, 1)
        assert lot.recorded_on == day(5)

    def test_zero_cost_lot(self, led: Ledger):
        txn(
            led,
            5,
            stock_leg("Assets:Brokerage", "10", cost=at_cost("0.00")),
            leg("Assets:Cash", "0.00"),
        )
        [lot] = led.list_lots("Assets:Brokerage")
        assert lot.cost == A("0.00")

    def test_inventory_shows_position_at_cost(self, led: Ledger):
        buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        inv = led.inventory("Assets:Brokerage")
        assert inv.positions() == [
            Position(
                units=A("10", AAPL),
                cost=Cost(per_unit=A("100.00"), date=day(5)),
            )
        ]
        assert led.balance("Assets:Brokerage").to_dict() == {"AAPL": Decimal("10")}

    def test_cost_commodity_must_be_registered(self, led: Ledger):
        with pytest.raises(UnknownCommodityError):
            txn(
                led,
                5,
                PostingSpec(
                    account="Assets:Brokerage",
                    units=A("10", AAPL),
                    cost=CostSpec(per_unit=Decimal("1"), commodity="EUR"),
                ),
                leg("Assets:Cash", "-10.00"),
            )

    def test_units_commodity_must_be_registered(self, led: Ledger):
        with pytest.raises(UnknownCommodityError):
            txn(
                led,
                5,
                stock_leg(
                    "Assets:Brokerage", "10", cost=at_cost("1.00"), commodity=MSFT
                ),
                leg("Assets:Cash", "-10.00"),
            )


class TestFifoSale:
    """The exit criterion (design §15): buy, partially sell under FIFO,
    and produce cost basis and realized gain matching Beancount exactly.
    Hand-computed: 12 sold at 130.00 -> proceeds 1560.00; FIFO basis
    10 x 100.00 + 2 x 120.00 = 1240.00; realized gain 320.00."""

    def sell_twelve(self, led: Ledger):
        buy(led, 5, "Assets:Fifo", "10", "100.00", "-1000.00")
        buy(led, 6, "Assets:Fifo", "5", "120.00", "-600.00")
        return txn(
            led,
            10,
            stock_leg("Assets:Fifo", "-12", cost=CostSpec(), price="130.00"),
            leg("Assets:Cash", "1560.00"),
            leg("Income:Gains:Fifo", None),
        )

    def test_realized_gain_is_a_posting(self, led: Ledger):
        sale = self.sell_twelve(led)
        gains = sale.postings[2]
        assert gains.interpolated
        assert gains.units == A("-320.00")
        assert led.balance("Income:Gains").to_dict() == {"USD": Decimal("-320.00")}

    def test_booking_consumed_oldest_first(self, led: Ledger):
        sale = self.sell_twelve(led)
        lots = led.list_lots("Assets:Fifo")
        assert sale.postings[0].lot_matches == (
            LotMatch(lots[0].id, A("10", AAPL).value),
            LotMatch(lots[1].id, A("2", AAPL).value),
        )
        assert sale.postings[0].weight == A("-1240.00")

    def test_remaining_inventory(self, led: Ledger):
        self.sell_twelve(led)
        assert led.inventory("Assets:Fifo").positions() == [
            Position(units=A("3", AAPL), cost=Cost(per_unit=A("120.00"), date=day(6)))
        ]

    def test_gains_account_was_auto_created(self, led: Ledger):
        self.sell_twelve(led)
        account = led.get_account("Income:Gains:Fifo")
        assert account is not None
        assert account.opened_on == day(10)

    def test_validator_clean_and_oracle_agrees(
        self, led: Ledger, assert_matches_beancount
    ):
        self.sell_twelve(led)
        assert led.validate().ok
        assert_matches_beancount(led)

    def test_dated_inventory_views(self, led: Ledger):
        self.sell_twelve(led)
        # before the sale both lots are whole
        assert led.inventory("Assets:Fifo", day(9)).positions() == [
            Position(units=A("10", AAPL), cost=Cost(per_unit=A("100.00"), date=day(5))),
            Position(units=A("5", AAPL), cost=Cost(per_unit=A("120.00"), date=day(6))),
        ]
        # before the second buy only the first lot exists
        assert led.inventory("Assets:Fifo", day(5)).positions() == [
            Position(units=A("10", AAPL), cost=Cost(per_unit=A("100.00"), date=day(5)))
        ]


class TestStrictBooking:
    def two_lots(self, led: Ledger):
        buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        buy(led, 6, "Assets:Brokerage", "5", "120.00", "-600.00")

    def test_ambiguous_partial_rejected_and_nothing_written(self, led: Ledger):
        self.two_lots(led)
        before = len(led.list_transactions())
        with pytest.raises(AmbiguousLotError):
            txn(
                led,
                10,
                stock_leg("Assets:Brokerage", "-4", cost=CostSpec(), price="130.00"),
                leg("Assets:Cash", "520.00"),
                leg("Income:Gains", None),
            )
        assert len(led.list_transactions()) == before
        assert led.balance("Assets:Brokerage").to_dict() == {"AAPL": Decimal("15")}
        # both lots still whole
        assert led.inventory("Assets:Brokerage").to_dict() == {"AAPL": Decimal("15")}

    def test_cost_filter_resolves(self, led: Ledger):
        self.two_lots(led)
        sale = txn(
            led,
            10,
            stock_leg("Assets:Brokerage", "-4", cost=at_cost("100.00"), price="130.00"),
            leg("Assets:Cash", "520.00"),
            leg("Income:Gains", None),
        )
        lots = led.list_lots("Assets:Brokerage")
        assert sale.postings[0].lot_matches == (
            LotMatch(lots[0].id, A("4", AAPL).value),
        )
        # gain: 4 x (130 - 100) = 120
        assert led.balance("Income:Gains").to_dict() == {"USD": Decimal("-120.00")}

    def test_date_filter_resolves(self, led: Ledger):
        self.two_lots(led)
        sale = txn(
            led,
            10,
            stock_leg(
                "Assets:Brokerage",
                "-3",
                cost=CostSpec(date=day(6)),
                price="130.00",
            ),
            leg("Assets:Cash", "390.00"),
            leg("Income:Gains", None),
        )
        lots = led.list_lots("Assets:Brokerage")
        assert sale.postings[0].lot_matches == (
            LotMatch(lots[1].id, A("3", AAPL).value),
        )

    def test_total_match_closes_the_position(self, led: Ledger):
        self.two_lots(led)
        txn(
            led,
            10,
            stock_leg("Assets:Brokerage", "-15", cost=CostSpec(), price="130.00"),
            leg("Assets:Cash", "1950.00"),
            leg("Income:Gains", None),
        )
        assert led.inventory("Assets:Brokerage").is_empty()
        # gain: 1950 - 1600 = 350
        assert led.balance("Income:Gains").to_dict() == {"USD": Decimal("-350.00")}

    def test_insufficient_rejected(self, led: Ledger):
        buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        with pytest.raises(InsufficientLotError):
            txn(
                led,
                10,
                stock_leg("Assets:Brokerage", "-12", cost=CostSpec(), price="130.00"),
                leg("Assets:Cash", "1560.00"),
                leg("Income:Gains", None),
            )

    def test_sale_before_the_lot_was_recorded_finds_nothing(self, led: Ledger):
        buy(led, 10, "Assets:Brokerage", "10", "100.00", "-1000.00")
        with pytest.raises(NoLotMatchError):
            # dated before the buy's transaction date, despite being
            # entered after it: the lot did not exist on day 7.
            txn(
                led,
                7,
                stock_leg("Assets:Brokerage", "-4", cost=CostSpec(), price="130.00"),
                leg("Assets:Cash", "520.00"),
                leg("Income:Gains", None),
            )

    def test_two_reductions_in_one_transaction_share_state(self, led: Ledger):
        buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        with pytest.raises(InsufficientLotError):
            txn(
                led,
                10,
                stock_leg("Assets:Brokerage", "-6", cost=CostSpec(), price="130.00"),
                stock_leg("Assets:Brokerage", "-6", cost=CostSpec(), price="130.00"),
                leg("Assets:Cash", "1560.00"),
                leg("Income:Gains", None),
            )
        sale = txn(
            led,
            10,
            stock_leg("Assets:Brokerage", "-6", cost=CostSpec(), price="130.00"),
            stock_leg("Assets:Brokerage", "-4", cost=CostSpec(), price="130.00"),
            leg("Assets:Cash", "1300.00"),
            leg("Income:Gains", None),
        )
        [lot] = led.list_lots("Assets:Brokerage")
        assert sale.postings[0].lot_matches == (LotMatch(lot.id, A("6", AAPL).value),)
        assert sale.postings[1].lot_matches == (LotMatch(lot.id, A("4", AAPL).value),)
        assert led.inventory("Assets:Brokerage").is_empty()

    def test_sale_at_exactly_cost_drops_the_open_gains_leg(self, led: Ledger):
        """Zero residual: the open gains posting is dropped, matching
        Beancount — the caller does not need to know in advance that the
        gain is zero."""
        buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        sale = txn(
            led,
            10,
            stock_leg("Assets:Brokerage", "-4", cost=CostSpec(), price="100.00"),
            leg("Assets:Cash", "400.00"),
            leg("Income:Gains", None),
        )
        assert [p.account for p in sale.postings] == [
            "Assets:Brokerage",
            "Assets:Cash",
        ]
        assert led.balance("Income:Gains").is_empty()
        assert led.validate().ok

    def test_none_booking_unsupported(self, led: Ledger):
        led.create_account("Assets:NoBook", "asset", JAN1, booking_method="NONE")
        buy(led, 5, "Assets:NoBook", "10", "100.00", "-1000.00")
        with pytest.raises(NotSupportedError):
            txn(
                led,
                10,
                stock_leg("Assets:NoBook", "-4", cost=CostSpec(), price="130.00"),
                leg("Assets:Cash", "520.00"),
                leg("Income:Gains", None),
            )


class TestGainsAccounts:
    def test_not_created_without_the_option(self, ledger: Ledger):
        ledger.create_commodity("USD", "currency")
        ledger.create_account("Assets:Cash", "asset", JAN1)
        with pytest.raises(UnknownAccountError):
            ledger.record(
                TransactionSpec(
                    date=day(5),
                    postings=[
                        leg("Assets:Cash", "-10.00"),
                        leg("Income:Gains", "10.00"),
                    ],
                )
            )

    def test_not_created_outside_the_root(self, led: Ledger):
        with pytest.raises(UnknownAccountError):
            txn(
                led,
                5,
                leg("Assets:Cash", "-10.00"),
                leg("Income:Dividends", "10.00"),
            )


class TestLotCorrections:
    """Design §11 with lots: reversing a reduction restores the lot,
    reversing an untouched acquisition consumes it, reversing a reduced
    acquisition is refused until its reductions are reversed."""

    def sold_four(self, led: Ledger):
        bought = buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        sale = txn(
            led,
            10,
            stock_leg("Assets:Brokerage", "-4", cost=CostSpec(), price="130.00"),
            leg("Assets:Cash", "520.00"),
            leg("Income:Gains", None),
        )
        return bought, sale

    def test_reversing_the_sale_restores_the_lot_for_reuse(self, led: Ledger):
        _bought, sale = self.sold_four(led)
        reversal = led.reverse(sale.id, day(12), "broker bust")
        [lot] = led.list_lots("Assets:Brokerage")
        assert reversal.postings[0].lot_matches == (
            LotMatch(lot.id, -A("4", AAPL).value),
        )
        assert led.inventory("Assets:Brokerage").positions() == [
            Position(units=A("10", AAPL), cost=Cost(per_unit=A("100.00"), date=day(5)))
        ]
        # the restored quantity can be sold again — full ten this time
        txn(
            led,
            15,
            stock_leg("Assets:Brokerage", "-10", cost=CostSpec(), price="140.00"),
            leg("Assets:Cash", "1400.00"),
            leg("Income:Gains", None),
        )
        assert led.inventory("Assets:Brokerage").is_empty()
        # gains: first sale -120, its reversal +120, second sale -400
        assert led.balance("Income:Gains").to_dict() == {"USD": Decimal("-400.00")}
        assert led.validate().ok

    def test_reversing_an_untouched_buy_consumes_its_lot(self, led: Ledger):
        bought = buy(led, 5, "Assets:Brokerage", "10", "100.00", "-1000.00")
        reversal = led.reverse(bought.id, day(8), "wrong account")
        [lot] = led.list_lots("Assets:Brokerage")
        assert reversal.postings[0].lot_matches == (
            LotMatch(lot.id, A("10", AAPL).value),
        )
        assert led.inventory("Assets:Brokerage").is_empty()
        assert led.balance("Assets:Cash").to_dict() == {"USD": Decimal("10000.00")}
        assert led.validate().ok

    def test_reversing_a_reduced_buy_is_refused(self, led: Ledger):
        bought, sale = self.sold_four(led)
        with pytest.raises(ReversalError):
            led.reverse(bought.id, day(12), "undo the buy")
        # reverse the dependent reduction first, then the buy goes through
        led.reverse(sale.id, day(12), "unwinding")
        led.reverse(bought.id, day(13), "undo the buy")
        assert led.inventory("Assets:Brokerage").is_empty()
        assert led.balance("Assets:Cash").to_dict() == {"USD": Decimal("10000.00")}
        assert led.balance("Income:Gains").is_empty()
        assert led.validate().ok

    def test_replace_rebooks_against_the_restored_lot(self, led: Ledger):
        _bought, sale = self.sold_four(led)
        # correction discovered: the sale was actually 6 shares at 125.00
        replacement = led.replace(
            sale.id,
            TransactionSpec(
                date=day(12),
                postings=[
                    stock_leg(
                        "Assets:Brokerage", "-6", cost=CostSpec(), price="125.00"
                    ),
                    leg("Assets:Cash", "750.00"),
                    leg("Income:Gains", None),
                ],
                narration="corrected sale",
            ),
        )
        [lot] = led.list_lots("Assets:Brokerage")
        assert replacement.postings[0].lot_matches == (
            LotMatch(lot.id, A("6", AAPL).value),
        )
        # gain now 6 x (125 - 100) = 150; the original's 120 was reversed
        assert led.balance("Income:Gains").to_dict() == {"USD": Decimal("-150.00")}
        assert led.inventory("Assets:Brokerage").positions() == [
            Position(units=A("4", AAPL), cost=Cost(per_unit=A("100.00"), date=day(5)))
        ]
        assert led.validate().ok

    def test_reversal_exports_and_matches_beancount(
        self, led: Ledger, assert_matches_beancount
    ):
        _bought, sale = self.sold_four(led)
        led.reverse(sale.id, day(12), "broker bust")
        assert_matches_beancount(led)


class TestAllowedCommodities:
    def test_units_commodity_still_enforced_for_cost_postings(self, led: Ledger):
        led.create_commodity("MSFT", "security", 0)
        led.create_account(
            "Assets:AppleOnly", "asset", JAN1, allowed_commodities=["AAPL"]
        )
        from ledger.domain.errors import CommodityNotAllowedError

        with pytest.raises(CommodityNotAllowedError):
            txn(
                led,
                5,
                stock_leg(
                    "Assets:AppleOnly", "10", cost=at_cost("1.00"), commodity=MSFT
                ),
                leg("Assets:Cash", "-10.00"),
            )
