"""Amount and Commodity: representation, round-trips, arithmetic."""

from decimal import Decimal

import pytest

from ledger.domain.amount import SCALE, Amount, Commodity, CommodityKind
from ledger.domain.errors import (
    AmountRangeError,
    CommodityMismatchError,
    InvalidCommodityError,
    PrecisionError,
)

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
EUR = Commodity("EUR", CommodityKind.CURRENCY, 2)
VACHR = Commodity("VACHR", CommodityKind.TRACKING, 0)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


class TestFromDecimal:
    def test_scale_8_representation(self):
        assert A("50.00").value == 5_000_000_000
        assert A("1.5").value == 150_000_000
        assert A("-3.14").value == -314_000_000
        assert A("0").value == 0

    @pytest.mark.parametrize(
        ("text", "precision"),
        [
            ("50", 0),
            ("50.0", 1),
            ("50.00", 2),
            ("50.000", 3),
            ("0.00000001", 8),
            ("1E+2", 0),  # positive exponent: no fractional digits
            ("-0.00", 2),
        ],
    )
    def test_precision_derived_from_exponent(self, text, precision):
        assert A(text).precision == precision

    def test_rejects_more_than_scale_decimals(self):
        with pytest.raises(PrecisionError):
            A("0.000000001")  # 9 decimals

    def test_rejects_non_finite(self):
        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(PrecisionError):
                A(bad)

    def test_rejects_int64_overflow(self):
        with pytest.raises(AmountRangeError):
            A("92233720368.54775808")  # 2**63 at scale 8
        # just inside the range is fine
        assert A("92233720368.54775807").value == 2**63 - 1


class TestToDecimal:
    @pytest.mark.parametrize(
        "text",
        ["0", "0.00", "-3.14", "50.00", "50.000", "123.45678901", "-0.5"],
    )
    def test_round_trip_value_and_exponent(self, text):
        d = Decimal(text)
        back = A(text).to_decimal()
        assert back == d
        assert back.as_tuple().exponent == d.as_tuple().exponent

    def test_never_truncates_significant_digits(self):
        # value needs 8 decimals but declared precision is 2
        amount = Amount.from_scaled(123_456_789, USD)  # 1.23456789
        assert amount.precision == 2
        assert amount.to_decimal() == Decimal("1.23456789")

    def test_pads_to_declared_precision(self):
        amount = Amount.from_scaled(5_000_000_000, USD)  # 50 at display 2
        assert str(amount.to_decimal()) == "50.00"


class TestArithmetic:
    def test_add_sub_neg_abs(self):
        assert (A("50.00") + A("25.50")).value == A("75.50").value
        assert (A("50.00") - A("75.00")).value == A("-25.00").value
        assert (-A("50.00")).value == -5_000_000_000
        assert abs(A("-1.00")) == A("1.00")

    def test_precision_of_sum_is_max(self):
        assert (A("50.0") + A("0.25")).precision == 2
        assert (-A("50.000")).precision == 3

    def test_cross_commodity_arithmetic_rejected(self):
        with pytest.raises(CommodityMismatchError):
            A("1.00") + A("1.00", EUR)
        with pytest.raises(CommodityMismatchError):
            A("1.00") - A("1.00", EUR)

    def test_multiply(self):
        assert A("10.05").multiply(Decimal(2)) == A("20.10")

    def test_multiply_rounds_half_even(self):
        one_unit = Amount.from_scaled(1, USD, precision=SCALE)
        three_units = Amount.from_scaled(3, USD, precision=SCALE)
        # 0.5 -> 0 (to even), 1.5 -> 2 (to even)
        assert one_unit.multiply(Decimal("0.5")).value == 0
        assert three_units.multiply(Decimal("0.5")).value == 2


class TestEquality:
    def test_equality_ignores_precision(self):
        assert A("50.0") == A("50.00")
        assert hash(A("50.0")) == hash(A("50.00"))

    def test_equality_respects_commodity(self):
        assert A("50.00") != A("50.00", EUR)

    def test_frozen(self):
        with pytest.raises(AttributeError):
            A("1.00").value = 7  # type: ignore[misc]


class TestCommodity:
    def test_valid_symbols(self):
        for symbol in ("USD", "V", "AAPL", "IRAUSD", "BRK.B", "X-1"):
            Commodity(symbol, CommodityKind.SECURITY)

    def test_invalid_symbols(self):
        for symbol in ("usd", "1USD", "", "USD-", "TOOLONGTOOLONGTOOLONGTOOLONG"):
            with pytest.raises(InvalidCommodityError):
                Commodity(symbol, CommodityKind.CURRENCY)

    def test_display_precision_bounds(self):
        with pytest.raises(InvalidCommodityError):
            Commodity("USD", CommodityKind.CURRENCY, 9)
        with pytest.raises(InvalidCommodityError):
            Commodity("USD", CommodityKind.CURRENCY, -1)

    def test_integer_commodity_round_trip(self):
        amount = A("5", VACHR)
        assert amount.value == 500_000_000
        assert str(amount.to_decimal()) == "5"
