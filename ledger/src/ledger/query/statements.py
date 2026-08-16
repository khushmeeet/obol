"""Balance sheet and income statement (design §3, plan §5).

Both reports are driven purely off `account.type`: the balance sheet is
Assets / Liabilities / Equity at a point in time, the income statement is
Income / Expenses over a date range. Each section is a tree of
`StatementNode`s built by prefix rollup over account paths, so a category
breakdown with drill-down is one call.

**Sign flipping for display lives here and only here.** The rest of the
system stays in raw signed amounts (design §4): positive means value
flowing into the account, so Liabilities, Equity and Income normally hold
negative balances. Reports negate those three types so that debt owed,
capital contributed, and income earned all read as positive numbers.

No closing entries exist, so accumulated earnings stay in Income and
Expense accounts rather than being folded into Equity. The equity section
shows only what was explicitly posted there (e.g. opening balances); the
headline number is `net_worth` = Assets + Liabilities, and the identity
`net_worth == equity (displayed) + net_income` holds per commodity.
"""

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ledger.domain.accounts import ROOT_FOR_TYPE, AccountType
from ledger.domain.amount import Amount, Commodity
from ledger.domain.errors import UnknownAccountError, UnknownCommodityError
from ledger.domain.inventory import Inventory

if TYPE_CHECKING:
    from ledger.storage.repositories import Repository

_NEGATED_FOR_DISPLAY = frozenset(
    {AccountType.LIABILITY, AccountType.EQUITY, AccountType.INCOME}
)


@dataclass(frozen=True, slots=True)
class StatementNode:
    """One account-path segment in a section's tree.

    `own` holds postings directly on this path; `total` rolls up the whole
    subtree. Both are display-signed. Intermediate path segments that were
    never opened as accounts still appear as nodes (the tree is the
    taxonomy); accounts whose every commodity nets to zero do not.
    """

    path: str
    name: str
    own: Inventory
    total: Inventory
    children: tuple[StatementNode, ...]

    def find(self, path: str) -> StatementNode | None:
        if path == self.path:
            return self
        if not path.startswith(self.path + ":"):
            return None
        for child in self.children:
            found = child.find(path)
            if found is not None:
                return found
        return None


@dataclass(frozen=True, slots=True)
class Section:
    """One account type's slice of a statement, display-signed."""

    title: str  # the root segment: "Assets", "Liabilities", ...
    type: AccountType
    total: Inventory
    children: tuple[StatementNode, ...]

    def find(self, path: str) -> StatementNode | None:
        for child in self.children:
            found = child.find(path)
            if found is not None:
                return found
        return None


@dataclass(frozen=True, slots=True)
class Statement:
    """Sections in a fixed order plus the report's headline number
    (`net`): net worth for a balance sheet, net income for an income
    statement. `start` and `end` are both inclusive; None means
    unbounded on that side."""

    start: datetime.date | None
    end: datetime.date | None
    sections: tuple[Section, ...]
    net: Inventory

    def section(self, type: AccountType) -> Section:
        for section in self.sections:
            if section.type is type:
                return section
        raise KeyError(f"statement has no {type.name} section")

    def find(self, path: str) -> StatementNode | None:
        for section in self.sections:
            if path.startswith(section.title + ":"):
                return section.find(path)
        return None


@dataclass(frozen=True, slots=True)
class BalanceSheet(Statement):
    @property
    def on(self) -> datetime.date | None:
        return self.end

    @property
    def assets(self) -> Section:
        return self.section(AccountType.ASSET)

    @property
    def liabilities(self) -> Section:
        return self.section(AccountType.LIABILITY)

    @property
    def equity(self) -> Section:
        return self.section(AccountType.EQUITY)

    @property
    def net_worth(self) -> Inventory:
        return self.net


@dataclass(frozen=True, slots=True)
class IncomeStatement(Statement):
    @property
    def income(self) -> Section:
        return self.section(AccountType.INCOME)

    @property
    def expenses(self) -> Section:
        return self.section(AccountType.EXPENSE)

    @property
    def net_income(self) -> Inventory:
        return self.net


def balance_sheet(
    repository: Repository,
    on: datetime.date | None = None,
) -> BalanceSheet:
    """Assets, Liabilities and Equity at end of day `on` (or over all time
    when None), with net worth = Assets + Liabilities."""
    wanted = (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY)
    by_type, commodities = _partition(repository, wanted, start=None, end=on)
    sections: list[Section] = []
    raw_by_type: dict[AccountType, dict[str, int]] = {}
    for type_ in wanted:
        section, raw = _build_section(type_, by_type[type_], commodities)
        sections.append(section)
        raw_by_type[type_] = raw
    net_raw: dict[str, int] = {}
    _merge(net_raw, raw_by_type[AccountType.ASSET])
    _merge(net_raw, raw_by_type[AccountType.LIABILITY])
    return BalanceSheet(
        start=None,
        end=on,
        sections=tuple(sections),
        net=_display(net_raw, commodities, negate=False),
    )


def income_statement(
    repository: Repository,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
) -> IncomeStatement:
    """Income and Expenses over [start, end] (both inclusive, either side
    unbounded when None), with net income positive when more was earned
    than spent."""
    wanted = (AccountType.INCOME, AccountType.EXPENSE)
    by_type, commodities = _partition(repository, wanted, start=start, end=end)
    sections: list[Section] = []
    net_raw: dict[str, int] = {}
    for type_ in wanted:
        section, raw = _build_section(type_, by_type[type_], commodities)
        sections.append(section)
        _merge(net_raw, raw)
    return IncomeStatement(
        start=start,
        end=end,
        sections=tuple(sections),
        # Raw income + expenses reads negative when money was made; the
        # negation makes net income positive for a profit.
        net=_display(net_raw, commodities, negate=True),
    )


# --- internals --------------------------------------------------------------


class _TreeBuilder:
    __slots__ = ("children", "own")

    def __init__(self) -> None:
        self.own: dict[str, int] = {}
        self.children: dict[str, _TreeBuilder] = {}


def _partition(
    repository: Repository,
    wanted: tuple[AccountType, ...],
    *,
    start: datetime.date | None,
    end: datetime.date | None,
) -> tuple[dict[AccountType, dict[str, dict[str, int]]], dict[str, Commodity]]:
    """Per-account raw balances in the window, split by account type, plus
    the commodities they mention."""
    balances = repository.balances_by_account(start=start, end=end)
    account_types = {
        account.path: account.type for account in repository.list_accounts()
    }
    by_type: dict[AccountType, dict[str, dict[str, int]]] = {
        type_: {} for type_ in wanted
    }
    commodities: dict[str, Commodity] = {}
    for path, by_symbol in balances.items():
        type_ = account_types.get(path)
        if type_ is None:  # cannot happen with intact referential integrity
            raise UnknownAccountError(path)
        if type_ in by_type:
            by_type[type_][path] = by_symbol
        for symbol in by_symbol:
            if symbol not in commodities:
                commodity = repository.get_commodity(symbol)
                if commodity is None:
                    raise UnknownCommodityError(symbol)
                commodities[symbol] = commodity
    return by_type, commodities


def _build_section(
    type_: AccountType,
    balances: dict[str, dict[str, int]],
    commodities: dict[str, Commodity],
) -> tuple[Section, dict[str, int]]:
    """The section tree plus its raw (unflipped) rollup, which the callers
    combine into net worth / net income."""
    root = ROOT_FOR_TYPE[type_]
    negate = type_ in _NEGATED_FOR_DISPLAY
    top = _TreeBuilder()
    for path, by_symbol in balances.items():
        node = top
        for segment in path.split(":")[1:]:
            node = node.children.setdefault(segment, _TreeBuilder())
        node.own = dict(by_symbol)
    children: list[StatementNode] = []
    total_raw: dict[str, int] = {}
    for name in sorted(top.children):
        child, child_raw = _freeze(
            f"{root}:{name}", name, top.children[name], commodities, negate
        )
        children.append(child)
        _merge(total_raw, child_raw)
    section = Section(
        title=root,
        type=type_,
        total=_display(total_raw, commodities, negate=negate),
        children=tuple(children),
    )
    return section, total_raw


def _freeze(
    path: str,
    name: str,
    builder: _TreeBuilder,
    commodities: dict[str, Commodity],
    negate: bool,
) -> tuple[StatementNode, dict[str, int]]:
    children: list[StatementNode] = []
    total_raw = dict(builder.own)
    for child_name in sorted(builder.children):
        child, child_raw = _freeze(
            f"{path}:{child_name}",
            child_name,
            builder.children[child_name],
            commodities,
            negate,
        )
        children.append(child)
        _merge(total_raw, child_raw)
    node = StatementNode(
        path=path,
        name=name,
        own=_display(builder.own, commodities, negate=negate),
        total=_display(total_raw, commodities, negate=negate),
        children=tuple(children),
    )
    return node, total_raw


def _merge(into: dict[str, int], other: dict[str, int]) -> None:
    for symbol, value in other.items():
        into[symbol] = into.get(symbol, 0) + value


def _display(
    raw: dict[str, int],
    commodities: dict[str, Commodity],
    *,
    negate: bool,
) -> Inventory:
    inventory = Inventory()
    for symbol, value in raw.items():
        if value == 0:
            continue
        inventory.add(
            Amount.from_scaled(-value if negate else value, commodities[symbol])
        )
    return inventory
