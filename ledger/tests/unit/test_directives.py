"""Pure assertion arithmetic (design §8): precision-derived tolerance and
the evaluation comparison. The rule was verified against Beancount 3.2.3:
one whole smallest written unit (twice the transaction tolerance),
inclusive boundary, zero for integers."""

from decimal import Decimal

from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.directives import (
    AssertionStatus,
    assertion_tolerance,
    evaluate_assertion,
)

USD = Commodity("USD", CommodityKind.CURRENCY, 2)


def usd(text: str) -> Amount:
    return Amount.from_decimal(Decimal(text), USD)


class TestAssertionTolerance:
    def test_two_decimals_tolerate_a_whole_cent(self):
        assert assertion_tolerance(2) == Decimal("0.01")

    def test_three_decimals(self):
        assert assertion_tolerance(3) == Decimal("0.001")

    def test_integer_precision_means_exact(self):
        assert assertion_tolerance(0) == Decimal(0)

    def test_multiplier_scales(self):
        assert assertion_tolerance(2, Decimal("2")) == Decimal("0.02")
        assert assertion_tolerance(2, Decimal("0")) == Decimal(0)

    def test_multiplier_does_not_rescue_integer_precision(self):
        assert assertion_tolerance(0, Decimal("10")) == Decimal(0)


class TestEvaluateAssertion:
    def test_exact_match_passes(self):
        status, difference = evaluate_assertion(usd("100.00"), 100_0000_0000)
        assert status is AssertionStatus.PASS
        assert difference.value == 0

    def test_difference_is_computed_minus_asserted(self):
        status, difference = evaluate_assertion(usd("100.00"), 102_5000_0000)
        assert status is AssertionStatus.FAIL
        assert difference.to_decimal() == Decimal("2.50")

    def test_boundary_is_inclusive(self):
        # Verified against Beancount: a 3-decimal assertion passes a
        # difference of exactly 0.001 and fails 0.0012.
        asserted = usd("100.000")
        on_boundary = 100_0010_0000
        past_boundary = 100_0012_0000
        assert evaluate_assertion(asserted, on_boundary)[0] is AssertionStatus.PASS
        assert evaluate_assertion(asserted, past_boundary)[0] is AssertionStatus.FAIL

    def test_two_decimal_assertion_tolerates_one_cent(self):
        assert (
            evaluate_assertion(usd("100.00"), 100_0100_0000)[0] is AssertionStatus.PASS
        )
        assert (
            evaluate_assertion(usd("100.00"), 100_0110_0000)[0] is AssertionStatus.FAIL
        )

    def test_integer_assertion_must_match_exactly(self):
        asserted = Amount.from_decimal(Decimal("100"), USD)
        assert asserted.precision == 0
        assert evaluate_assertion(asserted, 100_0000_0000)[0] is AssertionStatus.PASS
        assert evaluate_assertion(asserted, 100_0000_0001)[0] is AssertionStatus.FAIL

    def test_multiplier_loosens(self):
        asserted = usd("100.00")
        actual = 100_0150_0000  # off by 0.015
        assert evaluate_assertion(asserted, actual)[0] is AssertionStatus.FAIL
        assert (
            evaluate_assertion(asserted, actual, Decimal("2"))[0]
            is AssertionStatus.PASS
        )

    def test_negative_difference(self):
        status, difference = evaluate_assertion(usd("100.00"), 99_0000_0000)
        assert status is AssertionStatus.FAIL
        assert difference.to_decimal() == Decimal("-1.00")
