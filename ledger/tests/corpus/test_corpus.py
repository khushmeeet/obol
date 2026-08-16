"""M8 — the corpus gate (design §14 layer 3, plan §10).

For each committed bean-example fixture, the eight-step sequence from
design §14: import into a fresh ledger (both backends), assert zero
import errors, load the same file with Beancount, compare final balances
and lot-level inventories per account and commodity, compare realized
gains, compare market value at sampled dates against Beancount's own
price lookup, run validate(), then export back to Beancount text, reload
it, and assert everything still matches.

Pass bar: exact equality (design §14). Not approximate. A one-cent
discrepancy is a bug.

The fixtures are committed (plan §8.2) so an upstream bean-example
change cannot silently alter the corpus; regenerate deliberately with
tools/generate_corpus.sh.
"""

import datetime
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from beancount import loader
from beancount.core import data
from beancount.core import prices as bc_prices
from beancount.plugins import implicit_prices
from oracle import beancount_balances, beancount_lots, ledger_balances, ledger_lots

from ledger.api import Ledger
from ledger.storage.db import connect
from ledger.storage.repositories import InMemoryRepository

pytestmark = pytest.mark.corpus

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("corpus_*.beancount"))
BACKENDS = ("memory", "sqlite")


@pytest.fixture(scope="module", params=FIXTURES, ids=lambda path: path.stem)
def corpus(request):
    """One fixture file imported into both backends, plus Beancount's own
    load of it — computed once per file, shared by every step below."""
    path = request.param
    ledgers = {
        "memory": Ledger(repository=InMemoryRepository()),
        "sqlite": Ledger(connect(":memory:")),
    }
    reports = {name: led.import_beancount(path) for name, led in ledgers.items()}
    entries, errors, options = loader.load_file(str(path))
    return SimpleNamespace(
        path=path,
        ledgers=ledgers,
        reports=reports,
        entries=entries,
        load_errors=errors,
        options=options,
    )


# --- steps 1-3: import cleanly, on both backends -----------------------


def test_beancount_loads_the_fixture_clean(corpus):
    assert not corpus.load_errors, [str(e) for e in corpus.load_errors]


@pytest.mark.parametrize("backend", BACKENDS)
def test_import_reports_no_errors(corpus, backend):
    report = corpus.reports[backend]
    assert report.ok, report.errors[:10]
    assert not report.skipped, report.skipped[:10]


def test_backends_import_identically(corpus):
    reports = list(corpus.reports.values())
    assert reports[0].counts == reports[1].counts
    # Byte-identical export across backends is a suite-wide invariant.
    memory, sqlite = (corpus.ledgers[name] for name in BACKENDS)
    assert memory.export_beancount_string() == sqlite.export_beancount_string()


def test_every_directive_is_accounted_for(corpus):
    transactions = sum(
        1 for entry in corpus.entries if isinstance(entry, data.Transaction)
    )
    for report in corpus.reports.values():
        imported = report.counts.get("transaction", 0)
        padding = report.counts.get("padding-skipped", 0)
        assert imported + padding == transactions
        assert imported >= 1000  # a real multi-year corpus, not a stub


# --- step 4: final balances and lot inventories ------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_final_balances_match_beancount(corpus, backend):
    led = corpus.ledgers[backend]
    assert ledger_balances(led) == beancount_balances(corpus.entries)


@pytest.mark.parametrize("backend", BACKENDS)
def test_lot_inventories_match_beancount(corpus, backend):
    led = corpus.ledgers[backend]
    theirs = beancount_lots(corpus.entries)
    assert theirs, "corpus has no holdings at cost — not a real corpus"
    assert ledger_lots(led) == theirs


# --- step 5: realized gains per gains account --------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_realized_gains_match_beancount(corpus, backend):
    led = corpus.ledgers[backend]
    theirs = beancount_balances(corpus.entries)
    gains_accounts = {
        account
        for account, _currency in theirs
        if account.startswith("Income:")
        and any(part in ("PnL", "Gains") for part in account.split(":"))
    }
    assert gains_accounts, "corpus recorded no realized gains"
    for account in sorted(gains_accounts):
        ours = led.balance(account, include_children=False).to_dict()
        expected = {
            currency: total
            for (acct, currency), total in theirs.items()
            if acct == account
        }
        assert ours == expected, account


# --- step 6: market value at sampled dates -----------------------------


def _sampled_dates(entries):
    dates = [entry.date for entry in entries if isinstance(entry, data.Transaction)]
    first, last = min(dates), max(dates)
    samples = [datetime.date(year, 12, 31) for year in range(first.year, last.year + 1)]
    return [on for on in samples if on >= first] + [last]


@pytest.mark.parametrize("backend", BACKENDS)
def test_market_values_match_beancount(corpus, backend):
    led = corpus.ledgers[backend]
    operating = corpus.options["operating_currency"][0]

    # Beancount's price lookup, with the prices transactions imply made
    # explicit (their implicit_prices plugin) — the same observations
    # record() writes into the price table (design §9).
    entries_with_implied, plugin_errors = implicit_prices.add_implicit_prices(
        list(corpus.entries), corpus.options
    )
    assert not plugin_errors
    price_map = bc_prices.build_price_map(entries_with_implied)

    subtrees = sorted(
        {account.rsplit(":", 1)[0] for account in beancount_lots(corpus.entries)}
    )
    compared = 0
    for on in _sampled_dates(corpus.entries):
        for subtree in subtrees:
            quantities = defaultdict(Decimal)
            for entry in corpus.entries:
                if isinstance(entry, data.Transaction) and entry.date <= on:
                    for posting in entry.postings:
                        if posting.account == subtree or posting.account.startswith(
                            subtree + ":"
                        ):
                            quantities[posting.units.currency] += posting.units.number
            theirs = Decimal(0)
            missing_price = False
            for currency, quantity in quantities.items():
                if quantity == 0:
                    continue
                if currency == operating:
                    theirs += quantity
                    continue
                _date, price = bc_prices.get_price(price_map, (currency, operating), on)
                if price is None:
                    missing_price = True
                    break
                theirs += quantity * price
            if missing_price:
                continue
            ours = led.market_value(subtree, on, in_commodity=operating)
            assert ours.to_decimal() == theirs, (subtree, on)
            compared += 1
    assert compared >= len(subtrees)  # the guard above must not eat the test


# --- step 7: the full validator ----------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_validator_is_clean(corpus, backend):
    report = corpus.ledgers[backend].validate()
    assert report.ok, report.findings[:10]


# --- step 8: export, reload with Beancount, still exact ----------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_export_reloads_and_still_matches(corpus, backend):
    led = corpus.ledgers[backend]
    text = led.export_beancount_string()
    entries, errors, _options = loader.load_string(text)
    assert not errors, [str(e) for e in errors[:10]]
    assert beancount_balances(entries) == ledger_balances(led)
    assert beancount_lots(entries) == ledger_lots(led)
