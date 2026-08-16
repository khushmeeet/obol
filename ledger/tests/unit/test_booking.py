"""Booking resolution (design §7, plan §7.3).

The scenario table transcribes the behaviors verified against Beancount
3.2.3 (its booking_full / booking_method test enumeration, re-run locally
— scenarios ported, assertions our own): filters, position merging,
STRICT's total-match allowance, FIFO/LIFO ordering, and the three error
shapes (no match / ambiguous / not enough).
"""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.amount import SCALE, Amount, Commodity, CommodityKind
from ledger.domain.booking import (
    AvailableLot,
    BookingMethod,
    LotMatch,
    book_reduction,
)
from ledger.domain.errors import (
    AmbiguousLotError,
    InsufficientLotError,
    NoLotMatchError,
    NotSupportedError,
)
from ledger.domain.inventory import Cost, Lot

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
CAD = Commodity("CAD", CommodityKind.CURRENCY, 2)
AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def day(n: int) -> datetime.date:
    return datetime.date(2024, 1, n)


def lot(
    lot_id: int,
    quantity: str,
    cost: str,
    acquired: int,
    *,
    label: str | None = None,
    remaining: str | None = None,
    cost_commodity: Commodity = USD,
) -> AvailableLot:
    original = A(quantity, AAPL)
    left = A(remaining, AAPL) if remaining is not None else original
    return AvailableLot(
        lot=Lot(
            id=lot_id,
            account="Assets:Brokerage",
            commodity=AAPL,
            acquired_on=day(acquired),
            original_quantity=original.value,
            cost=A(cost, cost_commodity),
            label=label,
            recorded_on=day(acquired),
            opened_by_transaction_id=lot_id,
            opened_by_seq=0,
        ),
        remaining=left.value,
    )


EMPTY_SPEC = Cost()  # Beancount's {}


def book(available, units_text, spec=EMPTY_SPEC, method=BookingMethod.STRICT):
    return book_reduction(available, A(units_text, AAPL), spec, method)


TWO_LOTS = [lot(1, "10", "100.00", 5), lot(2, "5", "120.00", 6)]


class TestStrict:
    def test_single_lot_partial_reduction(self):
        booked = book([lot(1, "10", "100.00", 5)], "-4")
        assert booked.matches == (LotMatch(1, A("4", AAPL).value),)
        assert booked.weight == A("-400.00")
        assert booked.weight.precision == SCALE

    def test_ambiguous_partial_over_two_positions(self):
        with pytest.raises(AmbiguousLotError):
            book(TWO_LOTS, "-4")

    def test_total_match_consumes_every_position(self):
        # Verified: closing out the whole holding is never ambiguous.
        booked = book(TWO_LOTS, "-15")
        assert booked.matches == (
            LotMatch(1, A("10", AAPL).value),
            LotMatch(2, A("5", AAPL).value),
        )
        assert booked.weight == A("-1600.00")

    def test_total_match_of_filtered_subset(self):
        # Verified: the total-match rule applies to the *filtered* lots;
        # an unrelated position may remain.
        lots = [
            lot(1, "10", "100.00", 5),
            lot(2, "5", "100.00", 6),
            lot(3, "4", "120.00", 7),
        ]
        booked = book(lots, "-15", Cost(per_unit=A("100.00")))
        assert booked.matches == (
            LotMatch(1, A("10", AAPL).value),
            LotMatch(2, A("5", AAPL).value),
        )

    def test_cost_filter_disambiguates(self):
        booked = book(TWO_LOTS, "-4", Cost(per_unit=A("100.00")))
        assert booked.matches == (LotMatch(1, A("4", AAPL).value),)

    def test_date_filter_disambiguates(self):
        booked = book(TWO_LOTS, "-3", Cost(date=day(6)))
        assert booked.matches == (LotMatch(2, A("3", AAPL).value),)

    def test_label_filter_disambiguates(self):
        lots = [
            lot(1, "10", "100.00", 5, label="alpha"),
            lot(2, "5", "100.00", 6, label="beta"),
        ]
        booked = book(lots, "-3", Cost(label="beta"))
        assert booked.matches == (LotMatch(2, A("3", AAPL).value),)

    def test_currency_only_filter(self):
        booked = book([lot(1, "10", "100.00", 5)], "-3", Cost(commodity="USD"))
        assert booked.matches == (LotMatch(1, A("3", AAPL).value),)

    def test_same_key_lots_are_one_position(self):
        # Verified: two buys at the same (cost, date, label) merge; a
        # partial reduction across them is unambiguous under STRICT and
        # consumes in creation order.
        lots = [lot(1, "10", "100.00", 5), lot(2, "5", "100.00", 5)]
        booked = book(lots, "-12")
        assert booked.matches == (
            LotMatch(1, A("10", AAPL).value),
            LotMatch(2, A("2", AAPL).value),
        )

    def test_insufficient_in_matched_lot(self):
        with pytest.raises(InsufficientLotError):
            book([lot(1, "10", "100.00", 5)], "-12")

    def test_no_match(self):
        with pytest.raises(NoLotMatchError):
            book(TWO_LOTS, "-3", Cost(per_unit=A("99.00")))
        with pytest.raises(NoLotMatchError):
            book([], "-3")

    def test_wrong_cost_commodity_matches_nothing(self):
        with pytest.raises(NoLotMatchError):
            book(TWO_LOTS, "-3", Cost(per_unit=A("100.00", CAD)))

    def test_reduction_sees_remaining_not_original(self):
        booked = book([lot(1, "10", "100.00", 5, remaining="4")], "-4")
        assert booked.matches == (LotMatch(1, A("4", AAPL).value),)
        with pytest.raises(InsufficientLotError):
            book([lot(1, "10", "100.00", 5, remaining="4")], "-5")


class TestFifoLifo:
    def test_fifo_consumes_oldest_first(self):
        booked = book(TWO_LOTS, "-12", method=BookingMethod.FIFO)
        assert booked.matches == (
            LotMatch(1, A("10", AAPL).value),
            LotMatch(2, A("2", AAPL).value),
        )
        # 10 x 100.00 + 2 x 120.00
        assert booked.weight == A("-1240.00")

    def test_lifo_consumes_newest_first(self):
        booked = book(TWO_LOTS, "-7", method=BookingMethod.LIFO)
        assert booked.matches == (
            LotMatch(2, A("5", AAPL).value),
            LotMatch(1, A("2", AAPL).value),
        )
        # 5 x 120.00 + 2 x 100.00
        assert booked.weight == A("-800.00")

    def test_fifo_respects_filter(self):
        lots = [
            lot(1, "10", "100.00", 5),
            lot(2, "5", "120.00", 6),
            lot(3, "5", "100.00", 7),
        ]
        booked = book(
            lots, "-12", Cost(per_unit=A("100.00")), method=BookingMethod.FIFO
        )
        assert booked.matches == (
            LotMatch(1, A("10", AAPL).value),
            LotMatch(3, A("2", AAPL).value),
        )

    def test_fifo_insufficient_overall(self):
        with pytest.raises(InsufficientLotError):
            book(TWO_LOTS, "-16", method=BookingMethod.FIFO)


class TestSpecificAndNone:
    def test_specific_requires_a_named_lot(self):
        with pytest.raises(AmbiguousLotError):
            book([lot(1, "10", "100.00", 5)], "-3", method=BookingMethod.SPECIFIC)

    def test_specific_with_filter_resolves(self):
        booked = book(
            TWO_LOTS,
            "-3",
            Cost(per_unit=A("120.00")),
            method=BookingMethod.SPECIFIC,
        )
        assert booked.matches == (LotMatch(2, A("3", AAPL).value),)

    def test_specific_refuses_total_match_over_positions(self):
        with pytest.raises(AmbiguousLotError):
            book(
                TWO_LOTS,
                "-15",
                Cost(commodity="USD"),
                method=BookingMethod.SPECIFIC,
            )

    def test_none_is_not_supported(self):
        with pytest.raises(NotSupportedError):
            book(TWO_LOTS, "-3", method=BookingMethod.NONE)


class TestMixedCostCommodities:
    def test_reduction_across_cost_commodities_is_refused(self):
        lots = [
            lot(1, "10", "100.00", 5),
            lot(2, "5", "130.00", 6, cost_commodity=CAD),
        ]
        with pytest.raises(AmbiguousLotError):
            book(lots, "-15", method=BookingMethod.FIFO)

    def test_currency_filter_resolves_it(self):
        lots = [
            lot(1, "10", "100.00", 5),
            lot(2, "5", "130.00", 6, cost_commodity=CAD),
        ]
        booked = book(lots, "-5", Cost(commodity="CAD"), method=BookingMethod.FIFO)
        assert booked.matches == (LotMatch(2, A("5", AAPL).value),)
        assert booked.weight == A("-650.00", CAD)


class TestWeightArithmetic:
    def test_per_lot_products_round_half_even(self):
        # 0.15 shares x 0.10 USD = 0.015 -> two lots, each product rounds
        # half-even at scale 8 individually (design §5).
        shares = Commodity("FUND", CommodityKind.SECURITY, 8)
        tiny = AvailableLot(
            lot=Lot(
                id=1,
                account="Assets:Brokerage",
                commodity=shares,
                acquired_on=day(5),
                original_quantity=Amount.from_decimal(
                    Decimal("0.00000015"), shares
                ).value,
                cost=A("0.10"),
                recorded_on=day(5),
                opened_by_transaction_id=1,
                opened_by_seq=0,
            ),
            remaining=Amount.from_decimal(Decimal("0.00000015"), shares).value,
        )
        booked = book_reduction(
            [tiny],
            Amount.from_decimal(Decimal("-0.00000015"), shares),
            Cost(),
            BookingMethod.STRICT,
        )
        # exact product 0.000000015 -> rounds half-even to 0.00000002
        assert booked.weight.value == -2
