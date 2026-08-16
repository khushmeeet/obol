"""Two backends, one test suite (plan §1): every test that takes the
`ledger` fixture runs against both the in-memory and the SQLite
repository. Disagreement between the two is a bug in the SQLite layer.

Also home of the M2 oracle fixture, `assert_matches_beancount`, available
to every test from here on."""

import pathlib
import sys
from collections.abc import Iterator

import pytest

from ledger.api import Ledger
from ledger.storage.repositories import InMemoryRepository

# The golden scenario builders live beside the golden tests but are shared
# by integration tests (validator, CLI) too; tests/support holds the
# Beancount comparison helpers shared by the oracle fixture, the corpus
# gate, and the differential fuzz test.
sys.path.insert(0, str(pathlib.Path(__file__).parent / "golden"))
sys.path.insert(0, str(pathlib.Path(__file__).parent / "support"))


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest, tmp_path) -> Iterator[Ledger]:
    if request.param == "memory":
        yield Ledger(repository=InMemoryRepository())
    else:
        led = Ledger.open(tmp_path / "ledger.db")
        yield led
        led.close()


@pytest.fixture
def assert_matches_beancount():
    """The oracle harness (plan §2.2): export the ledger, load the text
    with Beancount, assert a clean load, and compare per-account,
    per-commodity balances — and, since M7, lot-level inventories —
    exactly.

    Compares balances and inventories, not directive structures —
    comparing our objects against Beancount's NamedTuples would need a
    translation layer that itself needs testing, and would couple the
    tests to their internals.
    """
    from beancount import loader
    from oracle import (
        beancount_balances,
        beancount_lots,
        ledger_balances,
        ledger_lots,
    )

    def check(led: Ledger) -> list:
        text = led.export_beancount_string()
        entries, errors, _options = loader.load_string(text)
        assert not errors, [str(error) for error in errors]
        assert ledger_balances(led) == beancount_balances(entries)
        assert ledger_lots(led) == beancount_lots(entries)
        return entries

    return check
