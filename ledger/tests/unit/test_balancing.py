"""Balancing, tolerance inference, and interpolation (design §6)."""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.balancing import (
    balance_transaction,
    compute_weight,
    infer_tolerances,
)
from ledger.domain.errors import (
    BookingError,
    InterpolationError,
    InvalidTransactionError,
    UnbalancedTransactionError,
)
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
JPY = Commodity("JPY", CommodityKind.CURRENCY, 0)
VACHR = Commodity("VACHR", CommodityKind.TRACKING, 0)
IRAUSD = Commodity("IRAUSD", CommodityKind.TRACKING, 2)

D = datetime.date(2024, 1, 15)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def spec(*postings: PostingSpec) -> TransactionSpec:
    return TransactionSpec(date=D, postings=list(postings))


def leg(account: str, text: str | None, commodity: Commodity = USD) -> PostingSpec:
    units = A(text, commodity) if text is not None else None
    return PostingSpec(account=account, units=units)


class TestToleranceInference:
    def test_half_of_smallest_written_unit(self):
        s = spec(leg("Assets:Cash", "-50.00"), leg("Expenses:Food", "50.00"))
        assert infer_tolerances(s) == {USD: Decimal("0.005")}

    def test_loosest_precision_wins_per_commodity(self):
        s = spec(leg("Assets:Cash", "-50.00"), leg("Expenses:Food", "50.004"))
        # 0.005 (from 2 decimals) > 0.0005 (from 3 decimals)
        assert infer_tolerances(s) == {USD: Decimal("0.005")}

    def test_integer_amounts_contribute_no_tolerance(self):
        s = spec(
            leg("Assets:Vacation", "5", VACHR), leg("Income:Vacation", "-5", VACHR)
        )
        assert infer_tolerances(s) == {}

    def test_per_commodity_independence(self):
        s = spec(
            leg("Assets:Cash", "-50.00"),
            leg("Expenses:Food", "50.00"),
            leg("Assets:Fund", "1.123", IRAUSD),
            leg("Income:X", "-1.123", IRAUSD),
        )
        assert infer_tolerances(s) == {
            USD: Decimal("0.005"),
            IRAUSD: Decimal("0.0005"),
        }

    def test_multiplier_scales_tolerance(self):
        s = spec(leg("Assets:Cash", "-50.00"), leg("Expenses:Food", "50.00"))
        assert infer_tolerances(s, Decimal(2)) == {USD: Decimal("0.010")}

    def test_omitted_postings_contribute_nothing(self):
        s = spec(leg("Assets:Cash", "-50.00"), leg("Expenses:Food", None))
        assert infer_tolerances(s) == {USD: Decimal("0.005")}


class TestBalancing:
    def test_two_posting_exact_balance(self):
        postings = balance_transaction(
            spec(leg("Assets:Cash", "-50.00"), leg("Expenses:Food", "50.00"))
        )
        assert len(postings) == 2
        assert sum(p.weight.value for p in postings) == 0
        assert all(not p.interpolated for p in postings)

    def test_weight_equals_units_without_cost_or_price(self):
        assert compute_weight(A("-50.00"), None, None) == A("-50.00")

    def test_residual_inside_tolerance_passes(self):
        balance_transaction(
            spec(leg("Assets:Cash", "-10.00"), leg("Expenses:Food", "10.004"))
        )

    def test_residual_at_tolerance_boundary_passes(self):
        balance_transaction(
            spec(leg("Assets:Cash", "-10.00"), leg("Expenses:Food", "10.005"))
        )

    def test_residual_outside_tolerance_rejected(self):
        with pytest.raises(UnbalancedTransactionError) as excinfo:
            balance_transaction(
                spec(leg("Assets:Cash", "-10.00"), leg("Expenses:Food", "10.006"))
            )
        assert excinfo.value.residuals == {"USD": Decimal("0.006")}
        assert excinfo.value.tolerances == {"USD": Decimal("0.005")}

    def test_integer_commodity_must_balance_exactly(self):
        balance_transaction(
            spec(
                leg("Assets:Vacation", "5", VACHR), leg("Income:Vacation", "-5", VACHR)
            )
        )
        with pytest.raises(UnbalancedTransactionError):
            balance_transaction(
                spec(
                    leg("Assets:Vacation", "5", VACHR),
                    leg("Income:Vacation", "-4", VACHR),
                )
            )

    def test_each_commodity_balances_independently(self):
        # USD balances, VACHR does not: must be rejected (design §6).
        with pytest.raises(UnbalancedTransactionError) as excinfo:
            balance_transaction(
                spec(
                    leg("Assets:Cash", "-50.00"),
                    leg("Expenses:Food", "50.00"),
                    leg("Assets:Vacation", "5", VACHR),
                )
            )
        assert "VACHR" in str(excinfo.value)
        assert "USD" not in excinfo.value.residuals

    def test_tolerance_multiplier_loosens(self):
        s = spec(leg("Assets:Cash", "-10.00"), leg("Expenses:Food", "10.008"))
        with pytest.raises(UnbalancedTransactionError):
            balance_transaction(s)
        balance_transaction(s, tolerance_multiplier=Decimal(2))

    def test_fewer_than_two_postings_rejected(self):
        with pytest.raises(InvalidTransactionError):
            balance_transaction(spec(leg("Assets:Cash", "-50.00")))

    def test_zero_posting_allowed(self):
        postings = balance_transaction(
            spec(
                leg("Assets:Cash", "-50.00"),
                leg("Expenses:Food", "50.00"),
                leg("Expenses:Fees", "0.00"),
            )
        )
        assert len(postings) == 3


class TestInterpolation:
    def test_fills_one_open_leg(self):
        postings = balance_transaction(
            spec(leg("Assets:Cash", "-50.00"), leg("Expenses:Food", None))
        )
        filled = postings[1]
        assert filled.account == "Expenses:Food"
        assert filled.units == A("50.00")
        assert filled.units.precision == 2  # max written precision for USD
        assert filled.interpolated
        assert filled.weight == filled.units

    def test_fill_is_exact_not_tolerance_rounded(self):
        postings = balance_transaction(
            spec(
                leg("Assets:Cash", "-50.00"),
                leg("Assets:Cash", "-0.004"),
                leg("Expenses:Food", None),
            )
        )
        assert postings[2].units == A("50.004")

    def test_open_leg_absorbs_every_unbalanced_commodity(self):
        # One open posting expands to one filled posting per residual
        # commodity, matching Beancount.
        postings = balance_transaction(
            spec(
                leg("Assets:Cash", "-50.00"),
                leg("Assets:CashJP", "-1000", JPY),
                leg("Expenses:Travel", None),
            )
        )
        assert len(postings) == 4
        fills = [p for p in postings if p.interpolated]
        assert {
            (p.account, p.units.commodity.symbol, p.units.value) for p in fills
        } == {
            ("Expenses:Travel", "JPY", A("1000", JPY).value),
            ("Expenses:Travel", "USD", A("50.00").value),
        }

    def test_two_open_legs_rejected(self):
        with pytest.raises(InterpolationError):
            balance_transaction(
                spec(
                    leg("Assets:Cash", "-50.00"),
                    leg("Expenses:Food", None),
                    leg("Expenses:Rent", None),
                )
            )

    def test_open_leg_dropped_when_everything_balances(self):
        """Beancount drops the auto-posting when the residual is zero
        (verified against 3.2.3) — M1's rejection was a divergence, fixed
        in M7 because a sale at exactly cost basis must be recordable
        with its gains leg left open."""
        postings = balance_transaction(
            spec(
                leg("Assets:Cash", "-50.00"),
                leg("Expenses:Food", "50.00"),
                leg("Expenses:Rent", None),
            )
        )
        assert [p.account for p in postings] == ["Assets:Cash", "Expenses:Food"]
        assert not any(p.interpolated for p in postings)

    def test_dropped_open_leg_must_leave_two_postings(self):
        with pytest.raises(InvalidTransactionError):
            balance_transaction(
                spec(leg("Assets:Cash", "0.00"), leg("Expenses:Rent", None))
            )


class TestRequiresResolvedLegs:
    """balance_transaction without legs is the plain-amount path; a cost
    or price posting must never be silently weighted as plain (M7 — the
    api resolves legs before balancing)."""

    def test_cost_requires_resolution(self):
        s = spec(
            PostingSpec(
                account="Assets:Stock",
                units=A("5"),
                cost=CostSpec(per_unit=Decimal("100.00"), commodity="USD"),
            ),
            leg("Assets:Cash", "-500.00"),
        )
        with pytest.raises(BookingError):
            balance_transaction(s)

    def test_price_requires_resolution(self):
        s = spec(
            PostingSpec(account="Assets:Cash", units=A("-100.00"), price=A("1.10")),
            leg("Assets:CashEU", "90.91"),
        )
        with pytest.raises(BookingError):
            balance_transaction(s)


class TestPaycheck:
    """The 18-posting, three-commodity paycheck (design §4). One
    transaction, three commodities, each balancing independently."""

    def make_spec(self) -> TransactionSpec:
        return TransactionSpec(
            date=D,
            payee="Hooli",
            narration="Payroll",
            postings=[
                leg("Income:Hooli:Salary", "-4432.16"),
                leg("Income:Hooli:Bonus", "-183.22"),
                leg("Assets:BofA:Checking", None),  # interpolated: 1350.60
                leg("Assets:Vanguard:PreTax401k", "961.54"),
                leg("Assets:Vanguard:Match401k", "238.46"),
                leg("Expenses:Taxes:Federal", "1062.92"),
                leg("Expenses:Taxes:State", "561.29"),
                leg("Expenses:Taxes:SocSec", "286.15"),
                leg("Expenses:Taxes:Medicare", "106.62"),
                leg("Expenses:Taxes:SDI", "1.12"),
                leg("Expenses:Health:Insurance", "24.32"),
                leg("Expenses:Health:Dental", "2.90"),
                leg("Expenses:Health:Vision", "18.48"),
                leg("Expenses:Fees:Payroll", "0.98"),
                leg("Assets:Federal:PreTax401k", "-1200.00", IRAUSD),
                leg("Expenses:Taxes:PreTax401k", "1200.00", IRAUSD),
                leg("Assets:Hooli:Vacation", "5", VACHR),
                leg("Income:Hooli:Vacation", "-5", VACHR),
            ],
        )

    def test_balances_with_eighteen_postings(self):
        postings = balance_transaction(self.make_spec())
        assert len(postings) == 18

        sums: dict[str, int] = {}
        for p in postings:
            symbol = p.weight.commodity.symbol
            sums[symbol] = sums.get(symbol, 0) + p.weight.value
        assert sums == {"USD": 0, "IRAUSD": 0, "VACHR": 0}

    def test_interpolated_checking_leg(self):
        postings = balance_transaction(self.make_spec())
        checking = [p for p in postings if p.account == "Assets:BofA:Checking"]
        assert len(checking) == 1
        assert checking[0].units == A("1350.60")
        assert checking[0].interpolated

    def test_breaking_one_commodity_rejects_whole_transaction(self):
        s = self.make_spec()
        # Pin the open checking leg first, else it would absorb the break.
        s.postings[2] = leg("Assets:BofA:Checking", "1350.60")
        s.postings[-1] = leg("Income:Hooli:Vacation", "-4", VACHR)
        with pytest.raises(UnbalancedTransactionError) as excinfo:
            balance_transaction(s)
        assert excinfo.value.residuals == {"VACHR": Decimal("1")}
