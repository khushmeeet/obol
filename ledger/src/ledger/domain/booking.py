"""Booking: resolving a reduction against available lots (design §7).

Pure — the caller supplies the candidate lots with their remaining
quantities; resolution returns which lots are drawn down and by how much,
plus the reduction's weight (the sum of matched quantity × lot cost, in
the cost commodity). Resolution happens once, at write time, and is
stored (design §16): a later policy change must not rewrite history.

Semantics verified against Beancount 3.2.3:

- The written cost is a *filter*: any subset of {per-unit cost, currency,
  date, label} narrows the candidates; ``{}`` matches everything.
- Lots sharing (cost, date, label) are one *position*: two same-day buys
  at the same cost merge, and reducing across them is unambiguous.
- STRICT: the filter must leave exactly one position — except that a
  reduction consuming the *total* of every filtered position exactly is
  allowed (closing out a holding is never ambiguous).
- FIFO consumes oldest first, LIFO newest first, both splitting across
  positions as needed.
- Reducing more than the filtered lots hold is an error ("Not enough
  lots"); a filter matching nothing is an error ("No position matches").

STRICT is the ledger-wide default, matching Beancount — an ambiguous
reduction from an importer is a signal, not something to silently
resolve. SPECIFIC is Obol's own stricter variant: the filter must be
non-empty and name exactly one position. NONE (Beancount's "just add a
negative position") has no oracle-tested use case and stays unimplemented.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ledger.domain.amount import SCALE, Amount, scaled_product
from ledger.domain.errors import (
    AmbiguousLotError,
    InsufficientLotError,
    NoLotMatchError,
    NotSupportedError,
)
from ledger.domain.inventory import Cost, Lot


class BookingMethod(Enum):
    STRICT = "STRICT"
    FIFO = "FIFO"
    LIFO = "LIFO"
    SPECIFIC = "SPECIFIC"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class AvailableLot:
    """A candidate for reduction: a stored lot and how much of it is left
    (scale 8, positive)."""

    lot: Lot
    remaining: int


@dataclass(frozen=True, slots=True)
class LotMatch:
    """One lot's share of a posting's booking: positive `quantity`
    (scale 8) consumes the lot, negative restores it (reversals)."""

    lot_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class BookedReduction:
    """A resolved reduction: the postings's weight (negative, in the cost
    commodity) and the per-lot draw-downs behind it."""

    weight: Amount
    matches: tuple[LotMatch, ...]


def _matches_filter(lot: Lot, spec: Cost) -> bool:
    if spec.per_unit is not None and (
        lot.cost.value != spec.per_unit.value
        or lot.cost.commodity != spec.per_unit.commodity
    ):
        return False
    if spec.commodity is not None and lot.cost.commodity.symbol != spec.commodity:
        return False
    if spec.date is not None and lot.acquired_on != spec.date:
        return False
    if spec.label is not None and lot.label != spec.label:
        return False
    return True


def _describe(available: AvailableLot) -> str:
    lot = available.lot
    quantity = Decimal(available.remaining).scaleb(-SCALE).normalize()
    label = f', "{lot.label}"' if lot.label else ""
    return (
        f"{quantity} {lot.commodity.symbol}"
        f" {{{lot.cost.to_decimal()} {lot.cost.commodity.symbol},"
        f" {lot.acquired_on}{label}}}"
    )


def _consume(
    candidates: Sequence[AvailableLot], quantity: int
) -> tuple[list[LotMatch], int]:
    """Draw `quantity` from `candidates` in order. Returns the matches and
    the weight (positive, scale 8): each lot's draw is rounded half-even
    to scale 8 individually, the way Beancount's booking splits a
    reduction into one posting per lot."""
    matches: list[LotMatch] = []
    weight = 0
    left = quantity
    for candidate in candidates:
        if left == 0:
            break
        take = min(left, candidate.remaining)
        assert candidate.lot.id is not None
        matches.append(LotMatch(lot_id=candidate.lot.id, quantity=take))
        weight += scaled_product(take, candidate.lot.cost.value)
        left -= take
    assert left == 0  # the caller checked the total first
    return matches, weight


def book_reduction(
    available: Sequence[AvailableLot],
    units: Amount,
    spec: Cost,
    method: BookingMethod,
) -> BookedReduction:
    """Resolve a reduction posting (negative `units`) against `available`
    lots using the written cost filter `spec` and the account's booking
    `method`. Raises NoLotMatchError, InsufficientLotError, or
    AmbiguousLotError; NONE booking is not supported."""
    assert units.value < 0
    quantity = -units.value
    symbol = units.commodity.symbol

    if method is BookingMethod.NONE:
        raise NotSupportedError(
            "NONE booking (unmatched reductions) is not supported;"
            " no oracle-tested use case exists (design §7)"
        )

    candidates = [
        candidate
        for candidate in available
        if candidate.remaining > 0 and _matches_filter(candidate.lot, spec)
    ]
    if not candidates:
        raise NoLotMatchError(
            f"no lot matches the reduction of {units.to_decimal()} {symbol};"
            f" held: {', '.join(_describe(a) for a in available) or 'nothing'}"
        )
    candidates.sort(key=lambda a: (a.lot.acquired_on, a.lot.id or 0))

    cost_commodities = {c.lot.cost.commodity.symbol for c in candidates}
    if len(cost_commodities) > 1:
        # A posting has one weight; drawing across cost commodities would
        # need one weight per commodity. Narrow the filter ({USD}) instead.
        raise AmbiguousLotError(
            f"reduction of {units.to_decimal()} {symbol} matches lots held"
            f" in several cost commodities ({', '.join(sorted(cost_commodities))});"
            f" add a cost-commodity filter"
        )

    total = sum(candidate.remaining for candidate in candidates)
    if quantity > total:
        raise InsufficientLotError(
            f"not enough lots to reduce {units.to_decimal()} {symbol}:"
            f" matched {', '.join(_describe(a) for a in candidates)}"
        )

    if method in (BookingMethod.STRICT, BookingMethod.SPECIFIC):
        if method is BookingMethod.SPECIFIC and spec.is_empty():
            raise AmbiguousLotError(
                f"SPECIFIC booking requires the reduction of"
                f" {units.to_decimal()} {symbol} to name its lot"
                f" (cost, date, or label); an empty {{}} spec names none"
            )
        positions = {candidate.lot.position_key() for candidate in candidates}
        if len(positions) > 1 and not (
            method is BookingMethod.STRICT and quantity == total
        ):
            raise AmbiguousLotError(
                f"ambiguous reduction of {units.to_decimal()} {symbol}:"
                f" matches {', '.join(_describe(a) for a in candidates)}"
            )
    elif method is BookingMethod.LIFO:
        candidates.sort(key=lambda a: (a.lot.acquired_on, a.lot.id or 0), reverse=True)
    # FIFO keeps the (acquired_on, id) sort.

    matches, weight = _consume(candidates, quantity)
    return BookedReduction(
        weight=Amount(
            value=-weight,
            precision=SCALE,
            commodity=candidates[0].lot.cost.commodity,
        ),
        matches=tuple(matches),
    )
