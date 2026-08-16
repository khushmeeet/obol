"""Inventory: what an account holds — per commodity, and per lot (M7).

A `Position` is a quantity of a commodity, optionally held at a `Cost`.
An `Inventory` is a collection of positions keyed by (commodity, cost);
positions that net to exactly zero are dropped, matching Beancount. Plain
holdings (cash) are positions with no cost, so `balance()` keeps returning
the same aggregate view it always has, while `inventory()` exposes the
lot-level detail.

A `Lot` is a stored parcel of a commodity acquired at a specific cost on a
specific date (design §7), created by an acquisition posting and reduced —
or, on reversal, restored — by `LotReductionEntry` rows.
"""

import datetime
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal

from ledger.domain.amount import Amount, Commodity
from ledger.domain.errors import InvalidCostError


@dataclass(frozen=True, slots=True)
class Cost:
    """Cost attached to a posting or position.

    On an acquisition (and on a held position) it is fully resolved:
    `per_unit` and `date` are always set. On a reduction it is the lot
    filter exactly as written — any subset of fields, all-empty being
    Beancount's ``{}``. `commodity` is set only for a currency-only filter
    (``{USD}``); when `per_unit` is present, its commodity is authoritative.
    """

    per_unit: Amount | None = None
    commodity: str | None = None
    date: datetime.date | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.per_unit is not None and self.commodity is not None:
            raise InvalidCostError(
                "cost carries both a per-unit amount and a bare commodity;"
                " the per-unit amount already names its commodity"
            )

    @property
    def cost_commodity(self) -> str | None:
        if self.per_unit is not None:
            return self.per_unit.commodity.symbol
        return self.commodity

    def is_empty(self) -> bool:
        return (
            self.per_unit is None
            and self.commodity is None
            and self.date is None
            and self.label is None
        )


@dataclass(frozen=True, slots=True)
class Position:
    """A quantity of a commodity, optionally held at cost."""

    units: Amount
    cost: Cost | None = None


@dataclass(frozen=True, slots=True)
class Lot:
    """A stored parcel: `original_quantity` of `commodity` acquired at
    `cost` per unit on `acquired_on` (the lot date — cost identity, which
    an explicit cost date may backdate). `recorded_on` is the opening
    transaction's date, which is what existence checks use."""

    account: str
    commodity: Commodity
    acquired_on: datetime.date
    original_quantity: int  # scale 8, positive
    cost: Amount  # per unit, in the cost commodity
    label: str | None = None
    recorded_on: datetime.date | None = None
    opened_by_transaction_id: int | None = None
    opened_by_seq: int | None = None
    id: int | None = None

    def position_key(self) -> tuple[int, str, datetime.date, str | None]:
        """Lots sharing this key are one position to Beancount (verified:
        two same-day buys at the same cost merge, and a reduction over the
        merged position is unambiguous under STRICT)."""
        return (
            self.cost.value,
            self.cost.commodity.symbol,
            self.acquired_on,
            self.label,
        )


@dataclass(frozen=True, slots=True)
class LotReductionEntry:
    """One lot_reductions row: `quantity` (scale 8) is positive when a
    reduction consumed the lot, negative when a reversal restored it. The
    posting is identified by (transaction_id, seq); `date` is that
    transaction's date."""

    lot_id: int
    transaction_id: int
    seq: int
    quantity: int
    date: datetime.date
    id: int | None = None


def _cost_sort_key(cost: Cost | None) -> tuple[object, ...]:
    if cost is None:
        return (0,)
    return (
        1,
        cost.date or datetime.date.min,
        cost.per_unit.value if cost.per_unit is not None else 0,
        cost.cost_commodity or "",
        cost.label or "",
    )


class Inventory:
    __slots__ = ("_positions",)

    def __init__(self, amounts: Iterable[Amount] = ()) -> None:
        self._positions: dict[tuple[str, Cost | None], Amount] = {}
        for amount in amounts:
            self.add(amount)

    def add(self, amount: Amount, cost: Cost | None = None) -> None:
        key = (amount.commodity.symbol, cost)
        current = self._positions.get(key)
        total = amount if current is None else current + amount
        if total.value == 0:
            self._positions.pop(key, None)
        else:
            self._positions[key] = total

    def add_position(self, position: Position) -> None:
        self.add(position.units, position.cost)

    def get(self, symbol: str) -> Amount | None:
        """Total units of `symbol` across all cost buckets."""
        total: Amount | None = None
        for (key_symbol, _cost), amount in self._positions.items():
            if key_symbol == symbol:
                total = amount if total is None else total + amount
        if total is None or total.value == 0:
            return None
        return total

    def is_empty(self) -> bool:
        return not self._positions

    def positions(self) -> list[Position]:
        """Every position, ordered by (commodity, cost-less first, lot
        date, cost, label) for deterministic display."""
        return [
            Position(units=self._positions[key], cost=key[1])
            for key in sorted(
                self._positions, key=lambda k: (k[0], _cost_sort_key(k[1]))
            )
        ]

    def to_dict(self) -> dict[str, Decimal]:
        """Aggregate units per commodity, costs collapsed."""
        totals: dict[str, Amount] = {}
        for (symbol, _cost), amount in self._positions.items():
            current = totals.get(symbol)
            totals[symbol] = amount if current is None else current + amount
        return {
            symbol: amount.to_decimal()
            for symbol, amount in sorted(totals.items())
            if amount.value != 0
        }

    def __iter__(self) -> Iterator[Amount]:
        """Aggregate amounts per commodity, sorted by symbol — the
        cost-collapsed view `balance()` has always returned."""
        totals: dict[str, Amount] = {}
        for (symbol, _cost), amount in self._positions.items():
            current = totals.get(symbol)
            totals[symbol] = amount if current is None else current + amount
        for symbol in sorted(totals):
            if totals[symbol].value != 0:
                yield totals[symbol]

    def __len__(self) -> int:
        return len(self._positions)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Inventory):
            return NotImplemented
        return self._positions == other._positions

    def __repr__(self) -> str:
        parts = []
        for position in self.positions():
            text = f"{position.units.to_decimal()} {position.units.commodity.symbol}"
            if position.cost is not None and position.cost.per_unit is not None:
                cost = position.cost
                assert cost.per_unit is not None
                text += (
                    f" {{{cost.per_unit.to_decimal()}"
                    f" {cost.per_unit.commodity.symbol}, {cost.date}}}"
                )
            parts.append(text)
        return f"Inventory({', '.join(parts)})"
