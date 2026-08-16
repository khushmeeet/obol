"""Differential fuzzing (design §14 layer 4, plan §8.4).

Hypothesis generates random ledgers within the semantics Obol supports —
plain spending and income, FIFO stock trading with interpolated gains
legs, price directives, and balance assertions — then the whole ledger
is exported, loaded with Beancount, and compared: a clean load, exact
per-account balances, exact lot-level inventories, and a clean
validator. This finds the shapes bean-example does not generate.

The exporter stays a dumb serializer tested separately against
hand-written output (plan §2.3), so agreement here cannot come from a
shared misunderstanding between exporter and ledger.

The example budget is deliberately small for every-commit runs;
the nightly CI job raises it via LEDGER_FUZZ_EXAMPLES.
"""

import datetime
import os
from decimal import Decimal

from beancount import loader
from hypothesis import event, given, settings
from hypothesis import strategies as st
from oracle import beancount_balances, beancount_lots, ledger_balances, ledger_lots

from ledger.api import Ledger
from ledger.domain.accounts import AccountType
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.directives import AssertionStatus
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec
from ledger.storage.db import connect

FUZZ_EXAMPLES = int(os.environ.get("LEDGER_FUZZ_EXAMPLES", "25"))

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
STK = Commodity("STK", CommodityKind.SECURITY, 0)

START = datetime.date(2020, 1, 6)


def build_ledger() -> Ledger:
    led = Ledger(connect(":memory:"))
    led.create_commodity("USD", CommodityKind.CURRENCY, 2)
    led.create_commodity("STK", CommodityKind.SECURITY, 0)
    for path, type_ in [
        ("Assets:Cash", AccountType.ASSET),
        ("Assets:Broker", AccountType.ASSET),
        ("Income:Salary", AccountType.INCOME),
        ("Income:Gains", AccountType.INCOME),
        ("Expenses:Stuff", AccountType.EXPENSE),
    ]:
        booking = "FIFO" if path == "Assets:Broker" else "STRICT"
        led.create_account(path, type_, START, booking_method=booking)
    return led


def usd(cents: int) -> Amount:
    return Amount.from_decimal(Decimal(cents).scaleb(-2), USD)


def shares(count: int) -> Amount:
    return Amount.from_decimal(Decimal(count), STK)


@settings(max_examples=FUZZ_EXAMPLES, deadline=None)
@given(data=st.data())
def test_random_history_matches_beancount(data):
    """Whatever a random session records, Beancount agrees with the
    export: balances, lot inventories, and every exported assertion."""
    led = build_ledger()
    day = START
    held = 0  # STK shares across all lots
    cash = 0  # Assets:Cash balance, cents
    assertions = 0

    for step in range(data.draw(st.integers(min_value=0, max_value=12))):
        day += datetime.timedelta(days=data.draw(st.integers(0, 3)))
        op = data.draw(
            st.sampled_from(["spend", "income", "buy", "sell", "price", "assert"]),
            label=f"op{step}",
        )
        event(f"op:{op}" if op != "sell" or held else "op:sell-skipped")
        if op == "spend":
            cents = data.draw(st.integers(1, 500_00))
            interpolate = data.draw(st.booleans())
            counter = (
                PostingSpec(account="Expenses:Stuff")
                if interpolate
                else PostingSpec(account="Expenses:Stuff", units=usd(cents))
            )
            led.record(
                TransactionSpec(
                    date=day,
                    postings=[
                        PostingSpec(account="Assets:Cash", units=usd(-cents)),
                        counter,
                    ],
                    narration=f"spend {step}",
                )
            )
            cash -= cents
        elif op == "income":
            cents = data.draw(st.integers(1, 5_000_00))
            led.record(
                TransactionSpec(
                    date=day,
                    postings=[
                        PostingSpec(account="Assets:Cash", units=usd(cents)),
                        PostingSpec(account="Income:Salary", units=usd(-cents)),
                    ],
                    narration=f"income {step}",
                )
            )
            cash += cents
        elif op == "buy":
            count = data.draw(st.integers(1, 20))
            cost = data.draw(st.integers(1_00, 500_00))
            led.record(
                TransactionSpec(
                    date=day,
                    postings=[
                        PostingSpec(
                            account="Assets:Broker",
                            units=shares(count),
                            cost=CostSpec(
                                per_unit=Decimal(cost).scaleb(-2), commodity="USD"
                            ),
                        ),
                        PostingSpec(account="Assets:Cash", units=usd(-count * cost)),
                    ],
                    narration=f"buy {step}",
                )
            )
            held += count
            cash -= count * cost
        elif op == "sell" and held > 0:
            count = data.draw(st.integers(1, held))
            price = data.draw(st.integers(1_00, 500_00))
            led.record(
                TransactionSpec(
                    date=day,
                    postings=[
                        PostingSpec(
                            account="Assets:Broker",
                            units=shares(-count),
                            cost=CostSpec(),  # {} — FIFO decides
                            price=usd(price),
                        ),
                        PostingSpec(account="Assets:Cash", units=usd(count * price)),
                        PostingSpec(account="Income:Gains"),  # interpolated
                    ],
                    narration=f"sell {step}",
                )
            )
            held -= count
            cash += count * price
        elif op == "price":
            led.record_price(
                "STK",
                day,
                Decimal(data.draw(st.integers(1_00, 500_00))).scaleb(-2),
                "USD",
            )
        elif op == "assert":
            day += datetime.timedelta(days=1)
            stated = led.assert_balance(
                "Assets:Cash", day, Decimal(cash).scaleb(-2), "USD"
            )
            assert stated.status is AssertionStatus.PASS
            assertions += 1

    assert led.validate().ok

    text = led.export_beancount_string()
    entries, errors, _options = loader.load_string(text)
    assert not errors, [str(error) for error in errors]
    assert ledger_balances(led) == beancount_balances(entries)
    assert ledger_lots(led) == beancount_lots(entries)
    # Beancount re-checked every exported balance assertion during load
    # (a failing one would be a load error), so `not errors` above is
    # also its verdict on our assertion arithmetic.
    assert len(led.list_assertions()) == assertions
