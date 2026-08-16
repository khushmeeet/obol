"""Account paths, types, and lifetime."""

import datetime

import pytest

from ledger.domain.accounts import (
    Account,
    AccountType,
    is_descendant_of,
    parent_path,
    type_for_path,
    validate_path,
)
from ledger.domain.errors import (
    AccountError,
    AccountTypeMismatchError,
    InvalidAccountPathError,
)


class TestPathValidation:
    @pytest.mark.parametrize(
        "path",
        [
            "Assets:Checking",
            "Assets:US:BofA:Checking",
            "Liabilities:Card",
            "Equity:Opening-Balances",
            "Income:Hooli:Salary",
            "Expenses:Taxes:Y2024:Federal",
            "Assets:401k",  # segments may start with a digit
        ],
    )
    def test_valid(self, path):
        validate_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "Assets",  # bare root is not an account
            "assets:Checking",  # unknown root
            "Banking:Chase",  # unknown root
            "Assets:",  # empty segment
            "Assets::Checking",
            "Assets:checking",  # segment must start upper/digit
            "Assets:Che cking",  # no spaces
            "",
        ],
    )
    def test_invalid(self, path):
        with pytest.raises(InvalidAccountPathError):
            validate_path(path)

    def test_type_for_path(self):
        assert type_for_path("Assets:X") is AccountType.ASSET
        assert type_for_path("Liabilities:X") is AccountType.LIABILITY
        assert type_for_path("Equity:X") is AccountType.EQUITY
        assert type_for_path("Income:X") is AccountType.INCOME
        assert type_for_path("Expenses:X") is AccountType.EXPENSE


class TestPathHelpers:
    def test_parent_path(self):
        assert parent_path("Assets:A:B") == "Assets:A"
        assert parent_path("Assets:A") == "Assets"
        assert parent_path("Assets") is None

    def test_is_descendant_of(self):
        assert is_descendant_of("Assets:A:B", "Assets:A")
        assert is_descendant_of("Assets:A:B", "Assets")
        assert not is_descendant_of("Assets:A", "Assets:A")  # not itself
        assert not is_descendant_of("Assets:AB", "Assets:A")  # no prefix trap
        assert not is_descendant_of("Assets", "Assets:A")


class TestAccount:
    def test_type_must_agree_with_root(self):
        with pytest.raises(AccountTypeMismatchError):
            Account(
                path="Assets:Checking",
                type=AccountType.EXPENSE,
                opened_on=datetime.date(2024, 1, 1),
            )

    def test_close_before_open_rejected(self):
        with pytest.raises(AccountError):
            Account(
                path="Assets:Checking",
                type=AccountType.ASSET,
                opened_on=datetime.date(2024, 1, 5),
                closed_on=datetime.date(2024, 1, 4),
            )

    def test_is_open_on_boundaries(self):
        account = Account(
            path="Assets:Checking",
            type=AccountType.ASSET,
            opened_on=datetime.date(2024, 1, 5),
            closed_on=datetime.date(2024, 2, 1),
        )
        assert not account.is_open_on(datetime.date(2024, 1, 4))
        assert account.is_open_on(datetime.date(2024, 1, 5))  # open inclusive
        assert account.is_open_on(datetime.date(2024, 2, 1))  # close inclusive
        assert not account.is_open_on(datetime.date(2024, 2, 2))

    def test_never_closed_stays_open(self):
        account = Account(
            path="Assets:Checking",
            type=AccountType.ASSET,
            opened_on=datetime.date(2024, 1, 5),
        )
        assert account.is_open_on(datetime.date(2099, 1, 1))
