"""Weights, tolerance inference, and interpolation (design §6).

A transaction balances when, for each commodity independently, the sum of
posting weights is zero within that commodity's inferred tolerance.

Weights (design §6):
- no cost, no price -> weight = units
- cost present      -> weight = quantity × cost per unit, in the cost
  commodity (a reduction's cost comes from the lots booking matched,
  never from the price)
- price, no cost    -> weight = quantity × price, in the price commodity

Tolerance is inferred per transaction, per commodity, from the decimal
precision of the numbers actually written in that transaction: half of the
smallest written unit, taking the loosest (coarsest-precision) contribution
per commodity — Beancount's rule, adopted exactly. Amounts written with no
fractional digits contribute no tolerance, so a commodity whose amounts are
all integers must balance exactly.

Only *units* numbers contribute, each to its own units commodity — cost
and price numbers contribute nothing (verified against Beancount 3.2.3,
whose `infer_tolerance_from_cost` defaults to off; design §6's claim that
cost postings contribute their cost commodity's precision was wrong).
"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ledger.domain.amount import SCALE, Amount, Commodity, scaled_product
from ledger.domain.booking import BookedReduction
from ledger.domain.errors import (
    BookingError,
    InterpolationError,
    InvalidCostError,
    InvalidPriceError,
    InvalidTransactionError,
    NotSupportedError,
    UnbalancedTransactionError,
)
from ledger.domain.inventory import Cost
from ledger.domain.transaction import Posting, PostingSpec, TransactionSpec


@dataclass(frozen=True, slots=True)
class ResolvedLeg:
    """Per-posting resolution done before balancing, parallel to
    `spec.postings`: the normalized cost and price, and — for reductions —
    the booking result carrying the weight and lot draw-downs."""

    cost: Cost | None = None
    price: Amount | None = None
    booking: BookedReduction | None = None


def resolve_leg(
    posting: PostingSpec,
    cost_commodity: Commodity | None,
    transaction_date: datetime.date,
) -> tuple[Cost | None, Amount | None]:
    """Validate and normalize one posting's cost and price as written.

    `cost_commodity` is the registered commodity for the cost spec's
    symbol (the caller resolves it; None when the spec names none).
    Returns (cost, price); the cost of an acquisition gets its lot date
    defaulted to the transaction date. Booking of reductions happens
    separately — the returned cost is the filter as written.
    """
    price = posting.price
    if price is not None and price.value < 0:
        # Beancount refuses negative prices; zero is allowed (verified).
        raise InvalidPriceError(f"negative prices are not allowed: {price!r}")
    if posting.units is None and (posting.cost is not None or price is not None):
        raise NotSupportedError(
            "interpolating a posting that carries a cost or price is not"
            " supported; give the posting its units"
        )

    spec = posting.cost
    if spec is None:
        return None, price
    assert posting.units is not None  # None with a cost raised above
    if posting.units.value == 0:
        raise InvalidCostError("a posting of zero units cannot carry a cost")
    if spec.per_unit is not None:
        if spec.commodity is None:
            raise InvalidCostError(f"cost {spec.per_unit} needs a commodity (e.g. USD)")
        if spec.per_unit < 0:
            raise InvalidCostError(f"cost is negative: {spec.per_unit}")
        assert cost_commodity is not None  # the caller resolved spec.commodity
        per_unit = Amount.from_decimal(spec.per_unit, cost_commodity)
    else:
        per_unit = None

    declared = spec.commodity if per_unit is None else None
    if price is not None:
        stated = spec.commodity or (
            per_unit.commodity.symbol if per_unit is not None else None
        )
        if stated is not None and stated != price.commodity.symbol:
            raise InvalidCostError(
                f"cost and price commodities must match:"
                f" {stated} != {price.commodity.symbol}"
            )

    if posting.units.value > 0:
        # An acquisition defines its lot: cost fully specified, lot date
        # defaulting to the transaction date (Beancount's rule).
        if per_unit is None:
            raise InvalidCostError(
                f"an acquisition of {posting.units!r} must state its cost"
                f" per unit; only reductions may use a partial cost filter"
            )
        return (
            Cost(
                per_unit=per_unit,
                date=spec.date or transaction_date,
                label=spec.label,
            ),
            price,
        )

    # A reduction: the cost is a lot filter, kept exactly as written.
    return (
        Cost(
            per_unit=per_unit,
            commodity=declared,
            date=spec.date,
            label=spec.label,
        ),
        price,
    )


def compute_weight(
    units: Amount | None,
    cost: Cost | None,
    price: Amount | None,
) -> Amount | None:
    """The amount a resolved posting contributes to the balancing check.

    Reductions are excluded: their weight comes from the lots that booking
    matched, supplied via ResolvedLeg.booking."""
    if units is None:
        return None
    if cost is not None:
        if units.value < 0:
            raise BookingError(
                "a cost reduction's weight comes from lot booking;"
                " resolve it with book_reduction first"
            )
        assert cost.per_unit is not None  # acquisitions are fully specified
        return Amount(
            value=scaled_product(units.value, cost.per_unit.value),
            precision=SCALE,
            commodity=cost.per_unit.commodity,
        )
    if price is not None:
        return Amount(
            value=scaled_product(units.value, price.value),
            precision=SCALE,
            commodity=price.commodity,
        )
    return units


def infer_tolerances(
    spec: TransactionSpec,
    multiplier: Decimal = Decimal(1),
) -> dict[Commodity, Decimal]:
    """Per-commodity tolerances from the written decimal precisions.

    Each written units amount with p >= 1 decimal places contributes
    0.5 * 10^-p * multiplier to its own commodity; the loosest
    contribution per commodity wins. Commodities absent from the result
    have tolerance zero. Postings with omitted amounts are not "written"
    and contribute nothing; neither do cost or price numbers (module
    docstring)."""
    tolerances: dict[Commodity, Decimal] = {}
    for posting in spec.postings:
        if posting.units is None:
            continue
        precision = posting.units.precision
        if precision < 1:
            continue
        tolerance = Decimal(5).scaleb(-(precision + 1)) * multiplier
        current = tolerances.get(posting.units.commodity)
        if current is None or tolerance > current:
            tolerances[posting.units.commodity] = tolerance
    return tolerances


def balance_transaction(
    spec: TransactionSpec,
    *,
    tolerance_multiplier: Decimal = Decimal(1),
    legs: Sequence[ResolvedLeg] | None = None,
) -> list[Posting]:
    """Resolve a spec into committed postings, or raise.

    `legs`, when given, is parallel to `spec.postings` and carries each
    posting's normalized cost/price and (for reductions) its booking; when
    omitted, every posting must be resolvable purely (no cost, no price —
    the M1 path used by domain tests).

    Groups weights by commodity, fills at most one omitted posting with the
    exact per-commodity residuals (one committed posting per residual
    commodity, matching Beancount), then asserts every remaining residual
    is within tolerance. Never a silent plug: an unbalanced transaction
    with no open posting is rejected. The generated realized-gain leg of a
    sale is exactly such a fill (design §7): the stock leg's weight is at
    cost, the cash leg is at proceeds, and the residual lands on the open
    gains posting.
    """
    if len(spec.postings) < 2:
        raise InvalidTransactionError("a transaction needs at least two postings")
    if legs is None:
        for posting in spec.postings:
            if posting.cost is not None or posting.price is not None:
                # Silently treating a cost or price posting as plain would
                # compute the wrong weight; the caller must resolve first.
                raise BookingError(
                    "postings with cost or price need resolved legs"
                    " (resolve_leg / book_reduction); record() does this"
                )
        legs = [ResolvedLeg() for _ in spec.postings]
    assert len(legs) == len(spec.postings)

    missing = [p for p in spec.postings if p.units is None]
    if len(missing) > 1:
        raise InterpolationError("at most one posting may omit its amount")

    tolerances = infer_tolerances(spec, tolerance_multiplier)

    residuals: dict[Commodity, int] = {}
    # Fill precision comes from written *units* numbers, not weights: a
    # weight derived from cost or price is a scale-8 computation, and a
    # gains leg interpolated from it should still print like the cash
    # amounts around it (to_decimal never drops significant digits).
    units_precision: dict[Commodity, int] = {}
    weights: list[Amount | None] = []
    for posting, leg in zip(spec.postings, legs, strict=True):
        if posting.units is not None:
            units_precision[posting.units.commodity] = max(
                units_precision.get(posting.units.commodity, 0),
                posting.units.precision,
            )
        if leg.booking is not None:
            weight: Amount | None = leg.booking.weight
        else:
            weight = compute_weight(posting.units, leg.cost, leg.price)
        weights.append(weight)
        if weight is None:
            continue
        commodity = weight.commodity
        residuals[commodity] = residuals.get(commodity, 0) + weight.value

    fills: dict[Commodity, Amount] = {}
    if missing:
        for commodity, residual in residuals.items():
            if residual != 0:
                fills[commodity] = Amount(
                    value=-residual,
                    precision=units_precision.get(commodity, 0),
                    commodity=commodity,
                )
        # When every commodity already balances, the open posting is
        # dropped, matching Beancount (verified against 3.2.3: the
        # auto-posting is removed, not zero-filled and not an error).
        # This is what lets a sale at exactly cost basis leave its gains
        # leg open like any other sale.
    else:
        failures: dict[str, Decimal] = {}
        failure_tolerances: dict[str, Decimal] = {}
        for commodity, residual in residuals.items():
            tolerance = tolerances.get(commodity, Decimal(0))
            if abs(Decimal(residual)) > tolerance.scaleb(SCALE):
                failures[commodity.symbol] = (
                    Decimal(residual).scaleb(-SCALE).normalize()
                )
                failure_tolerances[commodity.symbol] = tolerance
        if failures:
            raise UnbalancedTransactionError(failures, failure_tolerances)

    postings: list[Posting] = []
    for posting_spec, leg, weight in zip(spec.postings, legs, weights, strict=True):
        if posting_spec.units is None:
            for commodity in sorted(fills, key=lambda c: c.symbol):
                fill = fills[commodity]
                postings.append(
                    Posting(
                        account=posting_spec.account,
                        units=fill,
                        weight=fill,
                        flag=posting_spec.flag,
                        interpolated=True,
                        metadata=dict(posting_spec.metadata),
                    )
                )
        else:
            assert weight is not None
            postings.append(
                Posting(
                    account=posting_spec.account,
                    units=posting_spec.units,
                    weight=weight,
                    cost=leg.cost,
                    price=leg.price,
                    lot_matches=(
                        leg.booking.matches if leg.booking is not None else ()
                    ),
                    flag=posting_spec.flag,
                    interpolated=False,
                    metadata=dict(posting_spec.metadata),
                )
            )
    if len(postings) < 2:
        raise InvalidTransactionError(
            "a transaction needs at least two postings (the open posting"
            " was dropped because every commodity already balances)"
        )
    return postings
