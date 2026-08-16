"""Balance assertions and pads (design §8).

A balance assertion states that an account's balance in one commodity —
including sub-accounts — equals an amount at the *start* of a date
(postings dated that day are not counted). Its outcome is data (`status`,
`difference`), not an exception.

A pad is a directive that arms an automatic balancing transaction: when
the next assertion on that account is evaluated, the difference is booked
from a named source (equity) account, dated on the pad's date. This is the
mechanism for connecting an account that already has a balance and no full
history.

The evaluation arithmetic here is pure; fetching the actual balance and
writing results back is the API layer's job.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ledger.domain.amount import SCALE, Amount


class AssertionStatus(Enum):
    UNCHECKED = "unchecked"
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class BalanceAssertion:
    """`amount` is the asserted balance; its recorded precision drives the
    tolerance. `difference` is computed - asserted (positive: the account
    holds more than asserted), set when the assertion is checked."""

    date: datetime.date
    account: str
    amount: Amount
    source: str | None = None
    status: AssertionStatus = AssertionStatus.UNCHECKED
    difference: Amount | None = None
    checked_at: datetime.datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Pad:
    """`consumed_by` is the assertion that spent this pad — set whether or
    not padding was needed. `generated_txn_id` is the padding transaction,
    set only when the difference was outside tolerance."""

    date: datetime.date
    account: str
    source_account: str
    consumed_by: int | None = None
    generated_txn_id: int | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Note:
    """A dated comment attached to an account (design §10 — the hub
    attachments). Requires an account; an account-less dated fact is an
    Event."""

    date: datetime.date
    account: str
    comment: str
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """A dated file reference attached to an account: a path and an
    optional SHA-256 of the content (64 hex digits, stored lowercase).
    The library stores the reference only — it never manages file
    storage."""

    date: datetime.date
    account: str
    path: str
    sha256: str | None = None
    id: int | None = None


PRICE_ORIGINS = frozenset({"directive", "transaction", "fetch"})
"""Where a price row came from (design §9): an explicit directive, an
`@ price` (or acquisition cost) observed on a recorded transaction, or a
market-data fetch."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One row of the price table (design §9): `commodity` was worth
    `price` (an amount in the quote commodity, per unit) on `date`.
    Unique per (date, commodity, quote commodity)."""

    date: datetime.date
    commodity: str
    price: Amount
    origin: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """A dated fact about the ledger as a whole — 'employer', 'address' —
    with free-form type and value."""

    date: datetime.date
    type: str
    value: str
    id: int | None = None


def assertion_tolerance(precision: int, multiplier: Decimal = Decimal(1)) -> Decimal:
    """The tolerance an assertion written with `precision` decimals allows.

    Beancount's rule, verified against 3.2.3: one whole smallest written
    unit — *twice* the transaction-balancing tolerance — with an inclusive
    boundary, and zero for integer amounts. A two-decimal assertion
    tolerates a difference of exactly 0.01; 100 USD asserted as an integer
    must match exactly. (Design §8's "half the smallest unit" was wrong;
    Beancount doubles it for balance and pad "because the vast majority of
    people use these for bank accounts".)

    `multiplier` is the ledger's inferred_tolerance_multiplier, in Obol's
    convention where 1.0 reproduces Beancount's defaults.
    """
    if precision < 1:
        return Decimal(0)
    return Decimal(1).scaleb(-precision) * multiplier


def evaluate_assertion(
    asserted: Amount,
    actual_value: int,
    multiplier: Decimal = Decimal(1),
) -> tuple[AssertionStatus, Amount]:
    """Compare an asserted amount against the actual balance (a scale-8
    integer in the same commodity). Returns the status and the difference
    (computed - asserted). The boundary is inclusive: a difference of
    exactly one tolerance passes."""
    difference = Amount(
        value=actual_value - asserted.value,
        precision=asserted.precision,
        commodity=asserted.commodity,
    )
    tolerance = assertion_tolerance(asserted.precision, multiplier)
    within = abs(Decimal(difference.value)) <= tolerance.scaleb(SCALE)
    return (AssertionStatus.PASS if within else AssertionStatus.FAIL), difference
