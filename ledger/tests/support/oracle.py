"""Shared Beancount-comparison helpers (plan §2.2, §10).

Both sides of every comparison are reduced to plain dicts and sets of
scalars before comparing — balances as (account, currency) -> Decimal,
lot inventories as sets of (currency, quantity, cost, cost currency,
lot date, label) tuples per account. Comparing shapes this simple keeps
the tests decoupled from both Beancount's internal directive types and
the ledger's own domain objects.

Decimal values compare (and hash) numerically, so trailing-zero
differences between the two sides never produce false mismatches.
"""

from collections import defaultdict
from decimal import Decimal

from beancount.core import data
from beancount.core import inventory as bc_inventory

from ledger.api import Ledger

LotKey = tuple[str, Decimal, Decimal, str, object, object]


def beancount_balances(entries: list) -> dict[tuple[str, str], Decimal]:
    """(account, currency) -> summed units over all transactions, zeros
    dropped — Beancount's answer to `balance()`."""
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                totals[(posting.account, posting.units.currency)] += (
                    posting.units.number
                )
    return {key: total for key, total in totals.items() if total != 0}


def beancount_lots(entries: list) -> dict[str, set[LotKey]]:
    """account -> surviving positions held at cost, as scalar tuples —
    Beancount's answer to `inventory()`. Its own Inventory class does the
    position aggregation, so the reduction arithmetic is theirs."""
    inventories: dict[str, bc_inventory.Inventory] = defaultdict(bc_inventory.Inventory)
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                inventories[posting.account].add_amount(posting.units, posting.cost)
    lots: dict[str, set[LotKey]] = {}
    for account, inventory in inventories.items():
        keys = {
            (
                position.units.currency,
                position.units.number,
                position.cost.number,
                position.cost.currency,
                position.cost.date,
                position.cost.label,
            )
            for position in inventory
            if position.cost is not None
        }
        if keys:
            lots[account] = keys
    return lots


def ledger_balances(led: Ledger) -> dict[tuple[str, str], Decimal]:
    """(account, currency) -> balance, zeros dropped, per account (no
    rollups) — the shape beancount_balances produces."""
    totals: dict[tuple[str, str], Decimal] = {}
    for account in led.list_accounts():
        balances = led.balance(account.path, include_children=False).to_dict()
        for symbol, value in balances.items():
            totals[(account.path, symbol)] = value
    return totals


def ledger_lots(led: Ledger) -> dict[str, set[LotKey]]:
    """account -> surviving positions held at cost, from `inventory()`,
    as the same scalar tuples beancount_lots produces."""
    lots: dict[str, set[LotKey]] = {}
    for account in led.list_accounts():
        inventory = led.inventory(account.path, include_children=False)
        keys = {
            (
                position.units.commodity.symbol,
                position.units.to_decimal(),
                position.cost.per_unit.to_decimal(),
                position.cost.per_unit.commodity.symbol,
                position.cost.date,
                position.cost.label,
            )
            for position in inventory.positions()
            if position.cost is not None
        }
        if keys:
            lots[account.path] = keys
    return lots
