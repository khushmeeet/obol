"""Weights with cost and price, and cost/price validation (design §6, §7,
plan §7.1). The sale shape from design §4 — cost determines the weight,
price does not — is the load-bearing case."""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.amount import SCALE, Amount, Commodity, CommodityKind, scaled_product
from ledger.domain.balancing import (
    ResolvedLeg,
    balance_transaction,
    compute_weight,
    resolve_leg,
)
from ledger.domain.booking import BookedReduction, LotMatch
from ledger.domain.errors import (
    BookingError,
    InvalidCostError,
    InvalidPriceError,
    NotSupportedError,
    UnbalancedTransactionError,
)
from ledger.domain.inventory import Cost
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
EUR = Commodity("EUR", CommodityKind.CURRENCY, 2)
AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)

D = datetime.date(2024, 1, 15)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


class TestScaledProduct:
    def test_exact_products(self):
        assert scaled_product(A("10", AAPL).value, A("100.00").value) == (
            A("1000.00").value
        )
        assert scaled_product(A("-11", AAPL).value, A("169.83").value) == (
            A("-1868.13").value
        )

    def test_half_rounds_to_even(self):
        # 0.00000015 x 0.10 = 0.000000015 -> 0.00000002 (1 is odd)
        assert scaled_product(15, 10_000_000) == 2
        # 0.00000025 x 0.10 = 0.000000025 -> 0.00000002 (2 is even)
        assert scaled_product(25, 10_000_000) == 2
        assert scaled_product(-15, 10_000_000) == -2
        assert scaled_product(-25, 10_000_000) == -2


class TestComputeWeight:
    def test_acquisition_weight_is_units_times_cost(self):
        weight = compute_weight(
            A("18.572", AAPL), Cost(per_unit=A("30.96"), date=D), None
        )
        assert weight == A("574.98912")
        assert weight.commodity == USD
        assert weight.precision == SCALE

    def test_price_only_weight_is_units_times_price(self):
        weight = compute_weight(A("-100.00"), None, A("1.10", EUR))
        assert weight == A("-110.00", EUR)

    def test_cost_beats_price(self):
        # The sale shape (design §4): {cost} determines the weight, @ price
        # records the exchange and must not affect it.
        weight = compute_weight(
            A("10", AAPL), Cost(per_unit=A("100.00"), date=D), A("130.00")
        )
        assert weight == A("1000.00")

    def test_reduction_requires_booking(self):
        with pytest.raises(BookingError):
            compute_weight(A("-4", AAPL), Cost(per_unit=A("100.00")), None)


def resolve(units, *, cost=None, price=None, cost_commodity=USD):
    posting = PostingSpec(
        account="Assets:Brokerage", units=units, cost=cost, price=price
    )
    return resolve_leg(posting, cost_commodity, D)


class TestResolveLeg:
    def test_plain_posting_passes_through(self):
        assert resolve(A("-50.00")) == (None, None)

    def test_acquisition_gets_lot_date_defaulted(self):
        cost, price = resolve(
            A("10", AAPL),
            cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
        )
        assert cost == Cost(per_unit=A("100.00"), date=D)
        assert price is None

    def test_explicit_lot_date_survives(self):
        backdated = datetime.date(2023, 6, 1)
        cost, _ = resolve(
            A("10", AAPL),
            cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD", date=backdated),
        )
        assert cost is not None and cost.date == backdated

    def test_reduction_filter_kept_as_written(self):
        cost, _ = resolve(A("-4", AAPL), cost=CostSpec(), cost_commodity=None)
        assert cost == Cost()
        cost, _ = resolve(
            A("-4", AAPL),
            cost=CostSpec(commodity="USD", label="alpha"),
        )
        assert cost == Cost(commodity="USD", label="alpha")

    def test_acquisition_must_state_per_unit_cost(self):
        with pytest.raises(InvalidCostError):
            resolve(A("10", AAPL), cost=CostSpec(commodity="USD"))

    def test_cost_needs_a_commodity(self):
        with pytest.raises(InvalidCostError):
            resolve(
                A("10", AAPL),
                cost=CostSpec(per_unit=Decimal("100.00")),
                cost_commodity=None,
            )

    def test_negative_cost_refused(self):
        # Beancount: "Cost is negative" (verified); zero is allowed.
        with pytest.raises(InvalidCostError):
            resolve(
                A("10", AAPL),
                cost=CostSpec(per_unit=Decimal("-1.00"), commodity="USD"),
            )
        cost, _ = resolve(
            A("10", AAPL), cost=CostSpec(per_unit=Decimal("0.00"), commodity="USD")
        )
        assert cost is not None and cost.per_unit == A("0.00")

    def test_negative_price_refused_zero_allowed(self):
        # Beancount refuses negative prices; zero loads fine (verified).
        with pytest.raises(InvalidPriceError):
            resolve(A("10", AAPL), price=A("-1.00"))
        _, price = resolve(A("10", AAPL), price=A("0.00"))
        assert price == A("0.00")

    def test_cost_and_price_commodities_must_match(self):
        # Beancount: "Cost and price currencies must match" (verified).
        with pytest.raises(InvalidCostError):
            resolve(
                A("-4", AAPL),
                cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
                price=A("120.00", EUR),
            )

    def test_zero_units_cannot_carry_cost(self):
        # Beancount: "Amount is zero" (verified).
        with pytest.raises(InvalidCostError):
            resolve(A("0", AAPL), cost=CostSpec(per_unit=Decimal("1"), commodity="USD"))

    def test_interpolated_leg_cannot_carry_cost_or_price(self):
        with pytest.raises(NotSupportedError):
            resolve(None, cost=CostSpec(per_unit=Decimal("1"), commodity="USD"))
        with pytest.raises(NotSupportedError):
            resolve(None, price=A("1.10"))


class TestSaleBalancing:
    """The realized-gain mechanism (design §7): the stock leg's weight is
    its cost basis, the cash leg is the proceeds, and the open gains leg
    is interpolated with the difference."""

    def sale_spec(self, gains_units=None):
        return TransactionSpec(
            date=D,
            postings=[
                PostingSpec(
                    account="Assets:Brokerage",
                    units=A("-5", AAPL),
                    cost=CostSpec(),
                    price=A("150.00"),
                ),
                PostingSpec(account="Assets:Cash", units=A("750.00")),
                PostingSpec(account="Income:Gains", units=gains_units),
            ],
        )

    def legs(self):
        return [
            ResolvedLeg(
                cost=Cost(),
                price=A("150.00"),
                booking=BookedReduction(
                    weight=A("-500.00"),  # 5 x 100.00 cost basis
                    matches=(LotMatch(lot_id=1, quantity=A("5", AAPL).value),),
                ),
            ),
            ResolvedLeg(),
            ResolvedLeg(),
        ]

    def test_gains_leg_interpolated_from_cost_basis(self):
        postings = balance_transaction(self.sale_spec(), legs=self.legs())
        gains = postings[2]
        assert gains.account == "Income:Gains"
        assert gains.interpolated
        assert gains.units == A("-250.00")
        assert gains.units.precision == 2  # printed like the cash around it
        stock = postings[0]
        assert stock.weight == A("-500.00")
        assert stock.lot_matches == (LotMatch(1, A("5", AAPL).value),)
        assert stock.price == A("150.00")

    def test_explicit_wrong_gain_is_rejected(self):
        with pytest.raises(UnbalancedTransactionError):
            balance_transaction(self.sale_spec(A("-200.00")), legs=self.legs())

    def test_explicit_exact_gain_balances(self):
        postings = balance_transaction(self.sale_spec(A("-250.00")), legs=self.legs())
        assert not postings[2].interpolated
