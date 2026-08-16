"""Balance queries (design §3).

A balance is always a query over postings — never a stored number. It sums
posting units (what the account holds), not weights.
"""

import datetime
from typing import TYPE_CHECKING

from ledger.domain.amount import Amount
from ledger.domain.errors import UnknownCommodityError
from ledger.domain.inventory import Cost, Inventory
from ledger.query.valuation import lot_remainders

if TYPE_CHECKING:
    from ledger.storage.repositories import Repository


def balance_value_before(
    repository: Repository,
    account: str,
    date: datetime.date,
    symbol: str,
) -> int:
    """The account's scale-8 balance in one commodity at the *start* of
    `date` — postings dated `date` itself are not counted, sub-accounts
    are. This is the quantity a balance assertion (design §8) checks."""
    if date <= datetime.date.min:
        return 0
    totals = repository.balance(
        account, include_children=True, on=date - datetime.timedelta(days=1)
    )
    return totals.get(symbol, 0)


def balance(
    repository: Repository,
    account: str,
    on: datetime.date | None = None,
    *,
    include_children: bool = True,
) -> Inventory:
    """The account's holdings at end of day `on` (postings dated `on`
    included), or over all time when `on` is None.

    `account` is a prefix query: it need not itself be an opened account
    for its children to roll up (design §4 — rollups are prefix matches).
    """
    totals = repository.balance(account, include_children=include_children, on=on)
    inventory = Inventory()
    for symbol, value in totals.items():
        commodity = repository.get_commodity(symbol)
        if commodity is None:  # cannot happen with intact referential integrity
            raise UnknownCommodityError(symbol)
        inventory.add(Amount.from_scaled(value, commodity))
    return inventory


def inventory(
    repository: Repository,
    account: str,
    on: datetime.date | None = None,
    *,
    include_children: bool = True,
) -> Inventory:
    """The account's holdings at end of day `on`, lot by lot (design §13):
    each surviving lot is a position at its cost, and whatever the posting
    sums hold beyond the lots — cash, or cost-less units of a lot-tracked
    commodity — is a plain position."""
    totals = repository.balance(account, include_children=include_children, on=on)
    result = Inventory()
    lot_totals: dict[str, int] = {}
    for lot, remaining in lot_remainders(
        repository, account, include_children=include_children, on=on
    ):
        if remaining == 0:
            continue
        symbol = lot.commodity.symbol
        lot_totals[symbol] = lot_totals.get(symbol, 0) + remaining
        result.add(
            Amount.from_scaled(remaining, lot.commodity),
            Cost(per_unit=lot.cost, date=lot.acquired_on, label=lot.label),
        )
    for symbol, value in totals.items():
        residual = value - lot_totals.get(symbol, 0)
        if residual:
            commodity = repository.get_commodity(symbol)
            if commodity is None:  # cannot happen with referential integrity
                raise UnknownCommodityError(symbol)
            result.add(Amount.from_scaled(residual, commodity))
    return result
