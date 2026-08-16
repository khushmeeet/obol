"""Transactions and postings (design §4).

Input and committed types are deliberately distinct: `PostingSpec` /
`TransactionSpec` may be incomplete (an omitted amount means "interpolate
me"); `Posting` / `Transaction` are fully resolved and immutable.
`record()` is the only path from one to the other, which is what stops
half-resolved state leaking into storage.
"""

import datetime
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from ledger.domain.amount import Amount
from ledger.domain.booking import LotMatch
from ledger.domain.errors import InvalidLinkError, InvalidTagError
from ledger.domain.inventory import Cost

PAD_SOURCE = "pad"
"""`source` carried by padding transactions the ledger generates (design §8)."""

REVERSAL_SOURCE = "reversal"
"""`source` carried by reversal transactions created by reverse()/replace()
(design §11). The reversal's `source_ref` is the reversed transaction's id,
so the unique (source, source_ref) index enforces one reversal per
transaction at the storage level."""

RESERVED_SOURCES = frozenset({PAD_SOURCE, REVERSAL_SOURCE})
"""Sources the ledger itself writes; record() rejects specs that claim them."""

# Beancount's tag/link charset (verified against 3.2.3): letters, digits,
# dash, underscore, slash, dot. Anything else would be unexportable.
_TAG_LINK_RE = re.compile(r"[A-Za-z0-9\-_/.]+$")


def validate_tags(names: Iterable[str]) -> frozenset[str]:
    """Normalize a spec's tags to a frozenset, rejecting names Beancount
    cannot represent."""
    tags = frozenset(names)
    for name in tags:
        if not _TAG_LINK_RE.match(name):
            raise InvalidTagError(
                f"invalid tag {name!r}: tags are letters, digits, and -_/."
            )
    return tags


def validate_links(names: Iterable[str]) -> frozenset[str]:
    """Normalize a spec's links to a frozenset, rejecting names Beancount
    cannot represent."""
    links = frozenset(names)
    for name in links:
        if not _TAG_LINK_RE.match(name):
            raise InvalidLinkError(
                f"invalid link {name!r}: links are letters, digits, and -_/."
            )
    return links


@dataclass(frozen=True, slots=True)
class CostSpec:
    """Lot cost as written on a posting spec (design §7).

    On an acquisition (positive units) `per_unit` and `commodity` are
    required and `date` defaults to the transaction date. On a reduction
    (negative units) every field is an optional lot filter — Beancount's
    ``{}``, ``{100.00 USD}``, ``{USD}``, ``{2024-01-05}``, ``{"label"}``.
    """

    per_unit: Decimal | None = None
    commodity: str | None = None
    date: datetime.date | None = None
    label: str | None = None


@dataclass(slots=True)
class PostingSpec:
    """One leg of a transaction as supplied by the caller.

    `units=None` asks the ledger to interpolate this leg so the
    transaction balances (design §6)."""

    account: str
    units: Amount | None = None
    cost: CostSpec | None = None
    price: Amount | None = None
    flag: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class TransactionSpec:
    date: datetime.date
    postings: list[PostingSpec]
    flag: str = "*"
    payee: str | None = None
    narration: str | None = None
    tags: set[str] = field(default_factory=set)
    links: set[str] = field(default_factory=set)
    source: str | None = None
    source_ref: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Posting:
    """A committed, fully resolved leg. Order within the transaction is the
    position in `Transaction.postings` (stored as `seq`).

    `cost` is the cost as written (fully resolved on acquisitions, the
    filter on reductions); the resolved truth of a reduction lives in
    `lot_matches` — positive quantities consume lots, negative quantities
    (on reversals) restore them. `price` never affects the weight when a
    cost is present (design §4)."""

    account: str
    units: Amount
    weight: Amount
    cost: Cost | None = None
    price: Amount | None = None
    lot_matches: tuple[LotMatch, ...] = ()
    flag: str | None = None
    interpolated: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict, hash=False)


@dataclass(frozen=True, slots=True)
class Transaction:
    """A committed transaction. `id` is assigned by storage; a not-yet-stored
    transaction carries `id=None`."""

    date: datetime.date
    postings: tuple[Posting, ...]
    flag: str = "*"
    payee: str | None = None
    narration: str | None = None
    tags: frozenset[str] = frozenset()
    links: frozenset[str] = frozenset()
    source: str | None = None
    source_ref: str | None = None
    generated: bool = False
    reverses_id: int | None = None
    created_at: datetime.datetime | None = None
    id: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict, hash=False)
