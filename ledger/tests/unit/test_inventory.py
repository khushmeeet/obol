"""Inventory semantics: accumulation, zero-dropping, equality — and, as
of M7, lot-level positions keyed by (commodity, cost)."""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.errors import InvalidCostError
from ledger.domain.inventory import Cost, Inventory, Position

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
VACHR = Commodity("VACHR", CommodityKind.TRACKING, 0)
AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)

JAN5 = datetime.date(2024, 1, 5)
JAN6 = datetime.date(2024, 1, 6)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def C(cost: str, date: datetime.date = JAN5) -> Cost:
    return Cost(per_unit=A(cost), date=date)


def test_accumulates_per_commodity():
    inv = Inventory([A("10.00"), A("5.00"), A("3", VACHR)])
    assert inv.get("USD") == A("15.00")
    assert inv.get("VACHR") == A("3", VACHR)
    assert len(inv) == 2


def test_zero_positions_are_dropped():
    inv = Inventory([A("10.00"), A("-10.00")])
    assert inv.is_empty()
    assert inv.get("USD") is None


def test_iteration_sorted_by_symbol():
    inv = Inventory([A("1", VACHR), A("2.00")])
    assert [a.commodity.symbol for a in inv] == ["USD", "VACHR"]


def test_to_dict():
    inv = Inventory([A("10.50")])
    assert inv.to_dict() == {"USD": Decimal("10.50")}


def test_equality():
    assert Inventory([A("10.00")]) == Inventory([A("5.00"), A("5.00")])
    assert Inventory([A("10.00")]) != Inventory([A("9.00")])
    assert Inventory([A("10.00")]) != Inventory([A("10", VACHR)])
    assert Inventory() == Inventory([A("1.00"), A("-1.00")])


class TestLotPositions:
    def test_positions_are_keyed_by_cost(self):
        inv = Inventory()
        inv.add(A("10", AAPL), C("100.00"))
        inv.add(A("5", AAPL), C("120.00", JAN6))
        assert len(inv) == 2
        assert inv.get("AAPL") == A("15", AAPL)  # aggregate across lots
        assert inv.to_dict() == {"AAPL": Decimal("15")}

    def test_same_cost_key_accumulates(self):
        inv = Inventory()
        inv.add(A("10", AAPL), C("100.00"))
        inv.add(A("5", AAPL), C("100.00"))
        assert len(inv) == 1
        assert inv.positions() == [Position(units=A("15", AAPL), cost=C("100.00"))]

    def test_zero_drop_is_per_position(self):
        inv = Inventory()
        inv.add(A("10", AAPL), C("100.00"))
        inv.add(A("-10", AAPL), C("100.00"))
        inv.add(A("5", AAPL), C("120.00", JAN6))
        assert inv.positions() == [Position(units=A("5", AAPL), cost=C("120.00", JAN6))]

    def test_cost_and_plain_positions_coexist(self):
        inv = Inventory()
        inv.add(A("250.00"))
        inv.add(A("10", AAPL), C("100.00"))
        inv.add(A("-4", AAPL))  # cost-less units, a separate position
        assert len(inv) == 3
        assert inv.get("AAPL") == A("6", AAPL)
        # plain positions sort before cost positions within a commodity
        assert inv.positions() == [
            Position(units=A("-4", AAPL)),
            Position(units=A("10", AAPL), cost=C("100.00")),
            Position(units=A("250.00")),
        ]

    def test_iteration_stays_aggregated(self):
        inv = Inventory()
        inv.add(A("10", AAPL), C("100.00"))
        inv.add(A("5", AAPL), C("120.00", JAN6))
        inv.add(A("1.00"))
        assert [(a.commodity.symbol, a.to_decimal()) for a in inv] == [
            ("AAPL", Decimal("15")),
            ("USD", Decimal("1.00")),
        ]

    def test_offsetting_lots_vanish_from_aggregates(self):
        inv = Inventory()
        inv.add(A("10", AAPL), C("100.00"))
        inv.add(A("-10", AAPL))
        # two positions remain, but the commodity nets to zero
        assert len(inv) == 2
        assert inv.get("AAPL") is None
        assert inv.to_dict() == {}
        assert list(inv) == []


def test_cost_rejects_redundant_commodity():
    with pytest.raises(InvalidCostError):
        Cost(per_unit=A("100.00"), commodity="USD")


def test_cost_commodity_property():
    assert C("100.00").cost_commodity == "USD"
    assert Cost(commodity="EUR").cost_commodity == "EUR"
    assert Cost().cost_commodity is None
    assert Cost().is_empty() and not C("100.00").is_empty()
