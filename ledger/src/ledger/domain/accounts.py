"""Accounts: types, path handling, and lifetime (design §4).

Paths are colon-separated hierarchies — ``Assets:Banking:Chase:Checking``.
Rollups are prefix matches; the tree *is* the taxonomy. The root segment
must agree with the account type.
"""

import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from ledger.domain.booking import BookingMethod
from ledger.domain.errors import (
    AccountError,
    AccountTypeMismatchError,
    InvalidAccountPathError,
)


class AccountType(Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


TYPE_FOR_ROOT: dict[str, AccountType] = {
    "Assets": AccountType.ASSET,
    "Liabilities": AccountType.LIABILITY,
    "Equity": AccountType.EQUITY,
    "Income": AccountType.INCOME,
    "Expenses": AccountType.EXPENSE,
}

ROOT_FOR_TYPE: dict[AccountType, str] = {
    type_: root for root, type_ in TYPE_FOR_ROOT.items()
}

# Each non-root segment starts with an uppercase letter or digit and may
# continue with letters, digits, and dashes (Beancount-compatible).
_SEGMENT_RE = re.compile(r"[A-Z0-9][A-Za-z0-9-]*$")


def validate_path(path: str) -> None:
    """Raise InvalidAccountPathError unless `path` is structurally valid.

    A valid path has a recognised root plus at least one further segment,
    matching Beancount (bare "Assets" is not an account).
    """
    segments = path.split(":")
    if len(segments) < 2:
        raise InvalidAccountPathError(
            f"account path {path!r} needs a root and at least one segment"
        )
    if segments[0] not in TYPE_FOR_ROOT:
        raise InvalidAccountPathError(
            f"account path {path!r} must start with one of {sorted(TYPE_FOR_ROOT)}"
        )
    for segment in segments[1:]:
        if not _SEGMENT_RE.match(segment):
            raise InvalidAccountPathError(
                f"invalid segment {segment!r} in account path {path!r}"
            )


def root_of(path: str) -> str:
    return path.split(":", 1)[0]


def type_for_path(path: str) -> AccountType:
    validate_path(path)
    return TYPE_FOR_ROOT[root_of(path)]


def parent_path(path: str) -> str | None:
    """The path one level up, or None at the top ("Assets:Cash" -> "Assets")."""
    if ":" not in path:
        return None
    return path.rsplit(":", 1)[0]


def is_descendant_of(path: str, ancestor: str) -> bool:
    """Strict descent: a path is not a descendant of itself, and
    "Assets:AB" is not a descendant of "Assets:A"."""
    return path.startswith(ancestor + ":")


@dataclass(frozen=True, slots=True)
class Account:
    path: str
    type: AccountType
    opened_on: datetime.date
    closed_on: datetime.date | None = None
    booking_method: BookingMethod = BookingMethod.STRICT
    allowed_commodities: frozenset[str] | None = None
    metadata: Mapping[str, object] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        validate_path(self.path)
        expected = TYPE_FOR_ROOT[root_of(self.path)]
        if self.type is not expected:
            raise AccountTypeMismatchError(
                f"account {self.path!r} must have type {expected.name}, "
                f"got {self.type.name}"
            )
        if self.closed_on is not None and self.closed_on < self.opened_on:
            raise AccountError(
                f"account {self.path!r} closes {self.closed_on} "
                f"before it opens {self.opened_on}"
            )

    def is_open_on(self, on: datetime.date) -> bool:
        """Both the open and close dates are inclusive: the closing
        transaction may land on the close date itself."""
        if on < self.opened_on:
            return False
        return self.closed_on is None or on <= self.closed_on
