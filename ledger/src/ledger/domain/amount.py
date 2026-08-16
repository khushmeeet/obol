"""Amounts and commodities (design §5).

Every stored amount is an integer at a fixed global scale of 8 decimal
places: $50.00 is 5_000_000_000. All arithmetic is integer arithmetic at
that common scale. `Decimal` appears only at the boundary, converted in
exactly one place (`Amount.from_decimal` / `Amount.to_decimal`).

Alongside the value, each amount records `precision` — the number of
decimal places as originally written. Tolerance inference (design §6) and
faithful display both depend on it.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum

from ledger.domain.errors import (
    AmountRangeError,
    CommodityMismatchError,
    InvalidCommodityError,
    PrecisionError,
)

SCALE = 8
"""Global decimal scale for all stored amounts."""

_FACTOR: int = 10**SCALE
_INT64_MAX = 2**63 - 1

# Beancount-compatible commodity symbol: 1-24 chars, starts with an
# uppercase letter, ends with an uppercase letter or digit, interior may
# add digits and '._-.
_SYMBOL_RE = re.compile(r"[A-Z](?:[A-Z0-9'._-]{0,22}[A-Z0-9])?$")


def scaled_product(a: int, b: int) -> int:
    """(scale-8 × scale-8) -> scale-8: quantity times a per-unit rate.

    The intermediate product is exact (scale 16) and rounded half-even to
    scale 8 (design §5). Pure integer arithmetic — no Decimal context to
    overflow on int64 × int64.
    """
    product = a * b
    sign = -1 if product < 0 else 1
    magnitude = abs(product)
    quotient = magnitude // _FACTOR
    twice = (magnitude - quotient * _FACTOR) * 2
    if twice > _FACTOR or (twice == _FACTOR and quotient % 2):
        quotient += 1
    return sign * quotient


class CommodityKind(Enum):
    CURRENCY = "currency"
    SECURITY = "security"
    TRACKING = "tracking"


@dataclass(frozen=True, slots=True)
class Commodity:
    """A unit that amounts are denominated in.

    `tracking` covers non-monetary units (vacation hours, 401k contribution
    allowances) that flow through real double-entry transactions.
    """

    symbol: str
    kind: CommodityKind
    display_precision: int = 2

    def __post_init__(self) -> None:
        if not _SYMBOL_RE.match(self.symbol):
            raise InvalidCommodityError(f"invalid commodity symbol {self.symbol!r}")
        if not 0 <= self.display_precision <= SCALE:
            raise InvalidCommodityError(
                f"display_precision must be 0..{SCALE}, got {self.display_precision}"
            )


@dataclass(frozen=True, slots=True, eq=False)
class Amount:
    """A quantity of a commodity.

    Equality and hashing compare `value` and `commodity` only — precision is
    display/tolerance information, not part of the number's identity
    (Decimal("50.0") == Decimal("50.00"), and the same holds here).
    """

    value: int  # at SCALE decimal places
    precision: int  # decimal places as originally written
    commodity: Commodity

    def __post_init__(self) -> None:
        if not 0 <= self.precision <= SCALE:
            raise PrecisionError(f"precision must be 0..{SCALE}, got {self.precision}")
        if not -_INT64_MAX <= self.value <= _INT64_MAX:
            raise AmountRangeError(
                f"amount {self.value} at scale {SCALE} exceeds int64 range"
            )

    @classmethod
    def from_decimal(cls, d: Decimal, commodity: Commodity) -> Amount:
        if not d.is_finite():
            raise PrecisionError(f"amount must be finite, got {d}")
        exponent = d.as_tuple().exponent
        assert isinstance(exponent, int)  # finite implies numeric exponent
        precision = max(0, -exponent)
        if precision > SCALE:
            raise PrecisionError(
                f"{d} has {precision} decimal places; the ledger stores at most {SCALE}"
            )
        with localcontext() as ctx:
            ctx.prec = 38
            scaled = d.scaleb(SCALE)
        return cls(value=int(scaled), precision=precision, commodity=commodity)

    @classmethod
    def from_scaled(
        cls,
        value: int,
        commodity: Commodity,
        precision: int | None = None,
    ) -> Amount:
        """Build from an already-scaled integer (storage, derived balances).

        When `precision` is omitted, the commodity's display precision is
        used; `to_decimal` never drops significant digits regardless.
        """
        if precision is None:
            precision = commodity.display_precision
        return cls(value=value, precision=precision, commodity=commodity)

    def to_decimal(self) -> Decimal:
        """Exact Decimal value, with at least `precision` decimal places.

        Digits are padded to `precision` but never truncated: a value that
        needs more decimals than `precision` keeps them all.
        """
        digits = self.precision
        if self.value:
            while self.value % 10 ** (SCALE - digits):
                digits += 1
        return Decimal(self.value).scaleb(-SCALE).quantize(Decimal(1).scaleb(-digits))

    def _require_same_commodity(self, other: Amount) -> None:
        if self.commodity != other.commodity:
            raise CommodityMismatchError(
                f"cannot combine {self.commodity.symbol} with {other.commodity.symbol}"
            )

    def __add__(self, other: Amount) -> Amount:
        self._require_same_commodity(other)
        return Amount(
            value=self.value + other.value,
            precision=max(self.precision, other.precision),
            commodity=self.commodity,
        )

    def __sub__(self, other: Amount) -> Amount:
        self._require_same_commodity(other)
        return Amount(
            value=self.value - other.value,
            precision=max(self.precision, other.precision),
            commodity=self.commodity,
        )

    def __neg__(self) -> Amount:
        return Amount(
            value=-self.value, precision=self.precision, commodity=self.commodity
        )

    def __abs__(self) -> Amount:
        return Amount(
            value=abs(self.value),
            precision=self.precision,
            commodity=self.commodity,
        )

    def multiply(self, factor: Decimal) -> Amount:
        """quantity × factor, computed exactly and rounded half-even to
        SCALE (design §5). The result carries full stored precision."""
        with localcontext() as ctx:
            ctx.prec = 38
            product = (Decimal(self.value) * factor).quantize(
                Decimal(1), rounding=ROUND_HALF_EVEN
            )
        return Amount(value=int(product), precision=SCALE, commodity=self.commodity)

    def is_zero(self) -> bool:
        return self.value == 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Amount):
            return NotImplemented
        return self.value == other.value and self.commodity == other.commodity

    def __hash__(self) -> int:
        return hash((self.value, self.commodity))

    def __repr__(self) -> str:
        return f"Amount({self.to_decimal()} {self.commodity.symbol})"
