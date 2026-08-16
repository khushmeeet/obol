"""Property-based tests (plan §1.7).

Ledgers are built fresh inside each example (not via fixtures) so
Hypothesis can shrink freely, and every property runs against both
backends in lockstep — backend agreement itself is one of the invariants.
"""

import datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.api import Ledger
from ledger.domain.accounts import TYPE_FOR_ROOT
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.directives import AssertionStatus
from ledger.domain.errors import UnbalancedTransactionError
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec
from ledger.storage.db import connect
from ledger.storage.repositories import InMemoryRepository

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
JPY = Commodity("JPY", CommodityKind.CURRENCY, 0)
VTSAX = Commodity("VTSAX", CommodityKind.SECURITY, 4)
COMMODITIES = [USD, JPY, VTSAX]

ACCOUNTS = [
    "Assets:Checking",
    "Assets:Savings",
    "Liabilities:Card",
    "Income:Salary",
    "Expenses:Food",
    "Expenses:Home",
]

OPENED = datetime.date(2000, 1, 1)


def build_ledgers() -> list[Ledger]:
    ledgers = [
        Ledger(repository=InMemoryRepository()),
        Ledger(connect(":memory:")),
    ]
    for ledger in ledgers:
        for commodity in COMMODITIES:
            ledger.create_commodity(
                commodity.symbol, commodity.kind, commodity.display_precision
            )
        for path in ACCOUNTS:
            ledger.create_account(path, TYPE_FOR_ROOT[path.split(":")[0]], OPENED)
    return ledgers


def scaled_decimal(draw, precision: int) -> Decimal:
    raw = draw(st.integers(min_value=-(10**7), max_value=10**7))
    return Decimal(raw).scaleb(-precision)


@st.composite
def balanced_spec(draw, allow_interpolation: bool = True) -> TransactionSpec:
    """A transaction that balances by construction: for each chosen
    commodity, random legs plus one exact closing leg. Optionally the
    closing legs are replaced by a single open (interpolated) posting."""
    commodities = draw(
        st.lists(st.sampled_from(COMMODITIES), min_size=1, max_size=3, unique=True)
    )
    day = draw(st.dates(datetime.date(2020, 1, 1), datetime.date(2025, 12, 31)))

    postings: list[PostingSpec] = []
    closing: list[PostingSpec] = []
    any_residual = False
    for commodity in commodities:
        legs = draw(st.integers(min_value=1, max_value=4))
        total = Decimal(0)
        for _ in range(legs):
            value = scaled_decimal(draw, commodity.display_precision)
            total += value
            postings.append(
                PostingSpec(
                    account=draw(st.sampled_from(ACCOUNTS)),
                    units=Amount.from_decimal(value, commodity),
                )
            )
        if total != 0:
            any_residual = True
        closing.append(
            PostingSpec(
                account=draw(st.sampled_from(ACCOUNTS)),
                units=Amount.from_decimal(-total, commodity),
            )
        )

    interpolate = allow_interpolation and any_residual and draw(st.booleans())
    if interpolate:
        postings.append(PostingSpec(account=draw(st.sampled_from(ACCOUNTS))))
    else:
        postings.extend(closing)
    return TransactionSpec(date=day, postings=postings)


@settings(max_examples=60, deadline=None)
@given(specs=st.lists(balanced_spec(), min_size=1, max_size=5))
def test_accepted_transactions_balance_and_backends_agree(specs):
    ledgers = build_ledgers()
    expected: dict[tuple[str, str], int] = {}

    for spec in specs:
        recorded = []
        for ledger in ledgers:
            copied = TransactionSpec(
                date=spec.date,
                postings=[
                    PostingSpec(account=p.account, units=p.units) for p in spec.postings
                ],
            )
            recorded.append(ledger.record(copied))

        # Invariant 1: anything record() accepts has per-commodity weights
        # summing to exactly zero (fills are exact).
        for transaction in recorded:
            sums: dict[str, int] = {}
            for posting in transaction.postings:
                symbol = posting.weight.commodity.symbol
                sums[symbol] = sums.get(symbol, 0) + posting.weight.value
            assert all(value == 0 for value in sums.values())

        # Both backends committed identical postings.
        assert [
            (p.account, p.units.commodity.symbol, p.units.value)
            for p in recorded[0].postings
        ] == [
            (p.account, p.units.commodity.symbol, p.units.value)
            for p in recorded[1].postings
        ]

        for posting in recorded[0].postings:
            key = (posting.account, posting.units.commodity.symbol)
            expected[key] = expected.get(key, 0) + posting.units.value

    # Invariant 2: balance after N transactions equals the summed postings.
    for ledger in ledgers:
        for account in ACCOUNTS:
            inventory = ledger.balance(account)
            for commodity in COMMODITIES:
                want = expected.get((account, commodity.symbol), 0)
                got = inventory.get(commodity.symbol)
                assert (got.value if got is not None else 0) == want

    # Invariant 3: the backends agree on every journal.
    for account in ACCOUNTS:
        journals = [ledger.journal(account) for ledger in ledgers]
        assert [
            (e.date, e.posting.account, e.posting.units.value) for e in journals[0]
        ] == [(e.date, e.posting.account, e.posting.units.value) for e in journals[1]]


@settings(max_examples=60, deadline=None)
@given(spec=balanced_spec(allow_interpolation=False), data=st.data())
def test_perturbed_transactions_are_rejected_without_trace(spec, data):
    """Breaking one written leg by a whole unit lands outside every
    inferable tolerance (an open posting would absorb it, so these specs
    have none); the transaction must be rejected and leave no postings
    behind."""
    written = [i for i, p in enumerate(spec.postings) if p.units is not None]
    index = data.draw(st.sampled_from(written))
    broken = spec.postings[index]
    spec.postings[index] = PostingSpec(
        account=broken.account,
        units=broken.units + Amount.from_decimal(Decimal(1), broken.units.commodity),
    )

    for ledger in build_ledgers():
        with pytest.raises(UnbalancedTransactionError):
            ledger.record(spec)
        for account in ACCOUNTS:
            assert ledger.balance(account).is_empty()
            assert ledger.journal(account) == []


@settings(max_examples=60, deadline=None)
@given(specs=st.lists(balanced_spec(), min_size=1, max_size=5))
def test_statement_identity_and_backends_agree(specs):
    """For any recorded history: the backends produce identical statements,
    each section total equals the root rollup balance (display flip on
    Liabilities/Equity/Income only), and net worth == equity (displayed) +
    net income per commodity — the closing-entry-free accounting identity."""
    ledgers = build_ledgers()
    for spec in specs:
        for ledger in ledgers:
            copied = TransactionSpec(
                date=spec.date,
                postings=[
                    PostingSpec(account=p.account, units=p.units) for p in spec.postings
                ],
            )
            ledger.record(copied)

    sheets = [ledger.balance_sheet() for ledger in ledgers]
    stmts = [ledger.income_statement() for ledger in ledgers]
    assert sheets[0] == sheets[1]
    assert stmts[0] == stmts[1]

    def raw(inventory, symbol: str) -> int:
        amount = inventory.get(symbol)
        return amount.value if amount is not None else 0

    for ledger, sheet, stmt in zip(ledgers, sheets, stmts, strict=True):
        for commodity in COMMODITIES:
            s = commodity.symbol
            # Section totals are the rollup balances, display-flipped for
            # Liabilities / Equity / Income and raw elsewhere.
            assert raw(sheet.assets.total, s) == raw(ledger.balance("Assets"), s)
            assert raw(sheet.liabilities.total, s) == -raw(
                ledger.balance("Liabilities"), s
            )
            assert raw(sheet.equity.total, s) == -raw(ledger.balance("Equity"), s)
            assert raw(stmt.income.total, s) == -raw(ledger.balance("Income"), s)
            assert raw(stmt.expenses.total, s) == raw(ledger.balance("Expenses"), s)

            # The identity that replaces closing entries.
            assert raw(sheet.net_worth, s) == raw(sheet.equity.total, s) + raw(
                stmt.net_income, s
            )


@settings(max_examples=40, deadline=None)
@given(specs=st.lists(balanced_spec(), min_size=1, max_size=5))
def test_derived_assertions_pass_and_validator_runs_clean(specs):
    """For any recorded history: an assertion stating the exact computed
    balance passes, the full validator reports nothing, and both backends
    agree — record() never commits anything the validator would flag."""
    ledgers = build_ledgers()
    for spec in specs:
        for ledger in ledgers:
            copied = TransactionSpec(
                date=spec.date,
                postings=[
                    PostingSpec(account=p.account, units=p.units) for p in spec.postings
                ],
            )
            ledger.record(copied)

    after_everything = datetime.date(2026, 1, 1)
    for ledger in ledgers:
        for account in ACCOUNTS:
            inventory = ledger.balance(account)
            for commodity in COMMODITIES:
                amount = inventory.get(commodity.symbol)
                asserted = (
                    amount.to_decimal() if amount is not None else Decimal("0.00")
                )
                assertion = ledger.assert_balance(
                    account, after_everything, asserted, commodity.symbol
                )
                assert assertion.status is AssertionStatus.PASS
        report = ledger.validate()
        assert report.ok, str(report)


@settings(max_examples=40, deadline=None)
@given(specs=st.lists(balanced_spec(), min_size=1, max_size=5), data=st.data())
def test_reversal_restores_prior_balances(specs, data):
    """Design §14: reversing a transaction returns every affected balance
    to its value without that transaction, the reversal exactly negates
    it, and the validator stays clean — on both backends."""
    ledgers = build_ledgers()
    index = data.draw(st.integers(min_value=0, max_value=len(specs) - 1))
    on_date = max(spec.date for spec in specs)

    for ledger in ledgers:
        recorded = []
        for spec in specs:
            copied = TransactionSpec(
                date=spec.date,
                postings=[
                    PostingSpec(account=p.account, units=p.units) for p in spec.postings
                ],
            )
            recorded.append(ledger.record(copied))

        target = recorded[index]
        effect: dict[tuple[str, str], int] = {}
        for posting in target.postings:
            key = (posting.account, posting.units.commodity.symbol)
            effect[key] = effect.get(key, 0) + posting.units.value

        def balances(led):
            return {
                (account, commodity.symbol): (
                    amount.value
                    if (amount := led.balance(account).get(commodity.symbol))
                    else 0
                )
                for account in ACCOUNTS
                for commodity in COMMODITIES
            }

        before = balances(ledger)
        reversal = ledger.reverse(target.id, on_date, "property test")

        assert [
            (p.account, p.units.commodity.symbol, p.units.value)
            for p in reversal.postings
        ] == [
            (p.account, p.units.commodity.symbol, -p.units.value)
            for p in target.postings
        ]

        after = balances(ledger)
        for key in after:
            assert after[key] == before[key] - effect.get(key, 0)

        report = ledger.validate()
        assert report.ok, str(report)


TAG_POOL = ["trip", "work", "shared"]


@settings(max_examples=40, deadline=None)
@given(
    specs=st.lists(balanced_spec(), min_size=1, max_size=5),
    tag_sets=st.lists(
        st.sets(st.sampled_from(TAG_POOL), max_size=len(TAG_POOL)),
        min_size=5,
        max_size=5,
    ),
)
def test_tag_slices_match_hand_summed_postings(specs, tag_sets):
    """M6: for any recorded history with random tags, the tag slice is
    exactly the transactions recorded with that tag — list_transactions
    returns them, journal totals equal the hand-summed postings of the
    slice, and both backends agree."""
    ledgers = build_ledgers()
    tagged: dict[str, list[int]] = {tag: [] for tag in TAG_POOL}
    expected: dict[tuple[str, str, str], int] = {}

    for position, spec in enumerate(specs):
        tags = tag_sets[position]
        recorded = []
        for ledger in ledgers:
            copied = TransactionSpec(
                date=spec.date,
                postings=[
                    PostingSpec(account=p.account, units=p.units) for p in spec.postings
                ],
                tags=set(tags),
            )
            recorded.append(ledger.record(copied))
        assert all(t.tags == frozenset(tags) for t in recorded)
        for tag in tags:
            tagged[tag].append(position)
            for posting in recorded[0].postings:
                key = (tag, posting.account, posting.units.commodity.symbol)
                expected[key] = expected.get(key, 0) + posting.units.value

    for tag in TAG_POOL:
        listed = [ledger.list_transactions(tag=tag) for ledger in ledgers]
        assert len(listed[0]) == len(listed[1]) == len(tagged[tag])
        for ledger in ledgers:
            for account in ACCOUNTS:
                entries = ledger.journal(account, tag=tag, include_children=False)
                totals: dict[str, int] = {}
                for entry in entries:
                    symbol = entry.posting.units.commodity.symbol
                    totals[symbol] = totals.get(symbol, 0) + entry.posting.units.value
                for commodity in COMMODITIES:
                    want = expected.get((tag, account, commodity.symbol), 0)
                    assert totals.get(commodity.symbol, 0) == want


# --- M7: lots and booking ---------------------------------------------------

AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)


def build_lot_ledgers() -> list[Ledger]:
    """Both backends with a FIFO and a LIFO brokerage account, a gains
    root, and cash to trade against."""
    ledgers = [
        Ledger(repository=InMemoryRepository()),
        Ledger(connect(":memory:")),
    ]
    for ledger in ledgers:
        ledger.create_commodity("USD", "currency")
        ledger.create_commodity("AAPL", "security", 0)
        ledger.set_option("gains_account_root", "Income:Gains")
        ledger.create_account("Assets:Cash", "asset", OPENED)
        ledger.create_account("Assets:Fifo", "asset", OPENED, booking_method="FIFO")
        ledger.create_account("Assets:Lifo", "asset", OPENED, booking_method="LIFO")
    return ledgers


def _buy(ledger, account, date, shares: int, cost_cents: int):
    cost = Decimal(cost_cents).scaleb(-2)
    ledger.record(
        TransactionSpec(
            date=date,
            postings=[
                PostingSpec(
                    account=account,
                    units=Amount.from_decimal(Decimal(shares), AAPL),
                    cost=CostSpec(per_unit=cost, commodity="USD"),
                ),
                PostingSpec(
                    account="Assets:Cash",
                    units=Amount.from_decimal(-cost * shares, USD),
                ),
            ],
        )
    )


def _sell(ledger, account, date, shares: int, price_cents: int):
    price = Decimal(price_cents).scaleb(-2)
    return ledger.record(
        TransactionSpec(
            date=date,
            postings=[
                PostingSpec(
                    account=account,
                    units=Amount.from_decimal(Decimal(-shares), AAPL),
                    cost=CostSpec(),
                    price=Amount.from_decimal(price, USD),
                ),
                PostingSpec(
                    account="Assets:Cash",
                    units=Amount.from_decimal(price * shares, USD),
                ),
                PostingSpec(account="Income:Gains"),
            ],
        )
    )


@settings(max_examples=40, deadline=None)
@given(data=st.data())
def test_fifo_and_lifo_consume_identical_quantities(data):
    """Design §14 layer 2: for the same buys and the same sale, FIFO and
    LIFO consume identical total quantities and leave identical remaining
    totals — differing only in which lots (and so what cost basis / gain).
    The gain each reports must equal proceeds minus its own matched
    basis, recomputed by hand from the stored lots. Both backends agree."""
    buys = data.draw(
        st.lists(
            st.tuples(st.integers(1, 20), st.integers(1, 40000)),
            min_size=1,
            max_size=5,
        )
    )
    total = sum(shares for shares, _ in buys)
    sell = data.draw(st.integers(min_value=1, max_value=total))
    price_cents = data.draw(st.integers(1, 60000))

    for ledger in build_lot_ledgers():
        for offset, (shares, cost_cents) in enumerate(buys):
            date = datetime.date(2024, 1, 1) + datetime.timedelta(days=offset)
            _buy(ledger, "Assets:Fifo", date, shares, cost_cents)
            _buy(ledger, "Assets:Lifo", date, shares, cost_cents)
        sale_date = datetime.date(2024, 3, 1)
        gains: dict[str, int] = {}
        for account in ("Assets:Fifo", "Assets:Lifo"):
            sale = _sell(ledger, account, sale_date, sell, price_cents)
            stock = sale.postings[0]
            consumed = sum(match.quantity for match in stock.lot_matches)
            assert consumed == Amount.from_decimal(Decimal(sell), AAPL).value
            lots = {lot.id: lot for lot in ledger.list_lots(account)}
            basis = (
                sum(
                    match.quantity * lots[match.lot_id].cost.value
                    for match in stock.lot_matches
                )
                // 10**8
            )
            proceeds = Amount.from_decimal(
                Decimal(price_cents).scaleb(-2) * sell, USD
            ).value
            gains[account] = proceeds - basis
            assert stock.weight.value == -basis
            remaining = ledger.balance(account).get("AAPL")
            expected = total - sell
            assert (remaining.value if remaining else 0) == (
                Amount.from_decimal(Decimal(expected), AAPL).value
            )
        fifo_gain = ledger.balance("Income:Gains").get("USD")
        assert (fifo_gain.value if fifo_gain else 0) == -(
            gains["Assets:Fifo"] + gains["Assets:Lifo"]
        )
        report = ledger.validate()
        assert report.ok, str(report)


@settings(max_examples=30, deadline=None)
@given(data=st.data())
def test_random_fifo_trading_matches_a_model(data):
    """A model-based check: random buys and FIFO sales (kept within the
    quantity held), optionally ending with the last sale reversed. The
    surviving lots must equal a ten-line FIFO simulation, the validator
    stays clean, and both backends agree position for position."""
    steps = data.draw(st.integers(min_value=1, max_value=8))
    ledgers = build_lot_ledgers()
    # model: list of [cost_cents, remaining_shares] in acquisition order
    model: list[list[int]] = []
    day = datetime.date(2024, 1, 1)
    last_sale_shares: int | None = None

    for _ in range(steps):
        day += datetime.timedelta(days=1)
        held = sum(remaining for _, remaining in model)
        if held and data.draw(st.booleans()):
            shares = data.draw(st.integers(min_value=1, max_value=held))
            price_cents = data.draw(st.integers(1, 60000))
            for ledger in ledgers:
                _sell(ledger, "Assets:Fifo", day, shares, price_cents)
            left = shares
            for entry in model:
                take = min(left, entry[1])
                entry[1] -= take
                left -= take
            model = [entry for entry in model if entry[1]]
            last_sale_shares = shares
        else:
            shares = data.draw(st.integers(min_value=1, max_value=20))
            cost_cents = data.draw(st.integers(1, 40000))
            for ledger in ledgers:
                _buy(ledger, "Assets:Fifo", day, shares, cost_cents)
            model.append([cost_cents, shares])
            last_sale_shares = None

    positions = []
    for ledger in ledgers:
        report = ledger.validate()
        assert report.ok, str(report)
        held = ledger.balance("Assets:Fifo").get("AAPL")
        assert (held.value if held else 0) == Amount.from_decimal(
            Decimal(sum(remaining for _, remaining in model)), AAPL
        ).value
        positions.append(
            [
                (
                    position.units.to_decimal(),
                    position.cost.per_unit.to_decimal(),
                    position.cost.date,
                )
                for position in ledger.inventory("Assets:Fifo").positions()
            ]
        )
    assert positions[0] == positions[1]  # backends agree lot for lot

    if last_sale_shares is not None:
        # reversing the last sale restores the model state before it
        for ledger in ledgers:
            sale = ledger.list_transactions()[-1]
            ledger.reverse(sale.id, day + datetime.timedelta(days=1), "prop")
            report = ledger.validate()
            assert report.ok, str(report)
            held = ledger.balance("Assets:Fifo").get("AAPL")
            expected = sum(remaining for _, remaining in model) + last_sale_shares
            assert (held.value if held else 0) == Amount.from_decimal(
                Decimal(expected), AAPL
            ).value


@settings(max_examples=200, deadline=None)
@given(data=st.data())
def test_amount_decimal_round_trip(data):
    places = data.draw(st.integers(min_value=0, max_value=8))
    d = data.draw(
        st.decimals(
            min_value=Decimal(-(10**10)),
            max_value=Decimal(10**10),
            places=places,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    back = Amount.from_decimal(d, USD).to_decimal()
    assert back == d
    assert back.as_tuple().exponent == d.as_tuple().exponent
