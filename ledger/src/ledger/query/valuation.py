"""Market valuation from the price table (design §9).

Market value of a holding is quantity × the most recent price at or
before the valuation date. Unrealized gain is the market value of the
lots still held minus their cost basis — computed from the price table
against held lots, never posted (design §7).
"""

import datetime
from typing import TYPE_CHECKING

from ledger.domain.amount import Amount, scaled_product
from ledger.domain.errors import MissingPriceError, UnknownCommodityError
from ledger.domain.inventory import Lot

if TYPE_CHECKING:
    from ledger.storage.repositories import Repository


def _rate(
    repository: Repository,
    symbol: str,
    quote: str,
    on: datetime.date | None,
) -> int:
    """The scale-8 per-unit price of `symbol` in `quote`, most recent at
    or before `on` (latest known when None)."""
    point = repository.latest_price(symbol, quote, on)
    if point is None:
        when = f" at {on}" if on is not None else ""
        raise MissingPriceError(f"no price for {symbol} in {quote}{when}")
    return point.price.value


def market_value(
    repository: Repository,
    account: str,
    on: datetime.date | None = None,
    *,
    in_symbol: str,
) -> Amount:
    """The account's holdings (sub-accounts included) at end of day `on`,
    valued in `in_symbol`: holdings already in it count at face, every
    other commodity through the price table."""
    target = repository.get_commodity(in_symbol)
    if target is None:
        raise UnknownCommodityError(in_symbol)
    total = 0
    for symbol, value in repository.balance(
        account, include_children=True, on=on
    ).items():
        if symbol == in_symbol:
            total += value
        else:
            total += scaled_product(value, _rate(repository, symbol, in_symbol, on))
    return Amount.from_scaled(total, target)


def unrealized_gain(
    repository: Repository,
    account: str,
    on: datetime.date | None = None,
    *,
    in_symbol: str,
) -> Amount:
    """Market value minus cost basis of the lots held at end of day `on`,
    in `in_symbol`. A cost basis in another commodity is converted at the
    valuation-date rate (both sides of the difference are expressed at
    current prices)."""
    target = repository.get_commodity(in_symbol)
    if target is None:
        raise UnknownCommodityError(in_symbol)
    gain = 0
    for lot, remaining in lot_remainders(repository, account, on=on):
        if remaining == 0:
            continue
        value = scaled_product(
            remaining, _rate(repository, lot.commodity.symbol, in_symbol, on)
        )
        basis = scaled_product(remaining, lot.cost.value)
        if lot.cost.commodity.symbol != in_symbol:
            basis = scaled_product(
                basis, _rate(repository, lot.cost.commodity.symbol, in_symbol, on)
            )
        gain += value - basis
    return Amount.from_scaled(gain, target)


def lot_remainders(
    repository: Repository,
    account: str,
    *,
    symbol: str | None = None,
    include_children: bool = True,
    on: datetime.date | None = None,
) -> list[tuple[Lot, int]]:
    """Each lot under `account` with its remaining quantity at end of day
    `on`: lots recorded after `on` are excluded, and reductions (or
    restorations) dated after `on` are not counted. `on=None` is the
    current state. Lots reduced to zero are included with remaining 0 —
    callers filter."""
    lots = repository.list_lots(account, symbol, include_children=include_children)
    if on is not None:
        lots = [lot for lot in lots if lot.recorded_on and lot.recorded_on <= on]
    by_id = {lot.id: lot for lot in lots if lot.id is not None}
    remaining = {lot_id: lot.original_quantity for lot_id, lot in by_id.items()}
    for entry in repository.list_lot_reductions(lot_ids=set(by_id)):
        if on is not None and entry.date > on:
            continue
        remaining[entry.lot_id] -= entry.quantity
    return [(by_id[lot_id], remaining[lot_id]) for lot_id in sorted(by_id)]
