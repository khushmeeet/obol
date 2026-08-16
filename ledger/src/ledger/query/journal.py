"""Posting register (design §3)."""

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ledger.domain.transaction import Posting

if TYPE_CHECKING:
    from ledger.storage.repositories import Repository


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One posting with its transaction's header, for register display."""

    transaction_id: int
    date: datetime.date
    flag: str
    payee: str | None
    narration: str | None
    posting: Posting


def journal(
    repository: Repository,
    account: str,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    *,
    include_children: bool = True,
    tag: str | None = None,
    link: str | None = None,
) -> list[JournalEntry]:
    """Postings touching `account` (and, by default, its sub-accounts),
    ordered by date, then transaction, then position within the
    transaction. `start` and `end` are both inclusive.

    `tag` and `link` restrict the register to postings of transactions
    carrying them — the M6 slice across account boundaries: a tag groups
    transactions wherever they posted, and the register scopes that group
    to any subtree (e.g. all of `Expenses`)."""
    return repository.journal(
        account,
        include_children=include_children,
        start=start,
        end=end,
        tag=tag,
        link=link,
    )
