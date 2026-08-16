"""M2 export tests (plan §2.3).

Three gates per golden scenario, on both backends:

1. Byte-compare against hand-written expected output — the guard against
   the false-positive failure mode where a buggy exporter and a buggy
   ledger agree with each other.
2. The oracle: Beancount loads the export cleanly and computes exactly
   the same per-account, per-commodity balances.
3. `bean-check` (the real binary) passes on the exported file.
"""

import pathlib
import shutil
import subprocess

import pytest
from scenarios import (
    build_midlife_connection,
    build_month_of_spending,
    build_one_paycheck,
    build_stock_sale,
    build_tagged_trip,
    build_transfer,
)

EXPECTED_DIR = pathlib.Path(__file__).parent / "expected"

# The midlife_connection expected file has no padding transaction: used
# pads export as their `pad` directive with the generated transaction
# omitted, so Beancount regenerates the padding and the oracle comparison
# checks our pad arithmetic against its own.
SCENARIOS = {
    "midlife_connection": build_midlife_connection,
    "month_of_spending": build_month_of_spending,
    "paycheck": build_one_paycheck,
    "stock_sale": build_stock_sale,
    "tagged_trip": build_tagged_trip,
    "transfer": build_transfer,
}


@pytest.fixture(params=sorted(SCENARIOS))
def scenario(request, ledger):
    return request.param, SCENARIOS[request.param](ledger)


def test_export_matches_expected_bytes(scenario):
    name, led = scenario
    expected = (EXPECTED_DIR / f"{name}.beancount").read_text()
    assert led.export_beancount_string() == expected


def test_export_matches_beancount(scenario, assert_matches_beancount):
    _name, led = scenario
    assert_matches_beancount(led)


def test_oracle_catches_a_corrupted_amount(ledger):
    """The harness must be able to fail: doctor one digit in the exported
    text and Beancount must report the imbalance. Without this, a harness
    that always agrees proves nothing."""
    from beancount import loader

    led = build_month_of_spending(ledger)
    text = led.export_beancount_string()
    line = "  Assets:Checking  2500.00 USD"
    assert line in text
    doctored = text.replace(line, "  Assets:Checking  2500.01 USD")
    _entries, errors, _options = loader.load_string(doctored)
    assert errors


def test_bean_check_passes(scenario, tmp_path):
    name, led = scenario
    out = tmp_path / f"{name}.beancount"
    led.export_beancount(out)
    bean_check = shutil.which("bean-check")
    assert bean_check, "bean-check not found; is the dev environment synced?"
    result = subprocess.run(
        [bean_check, str(out)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_lot_inventories_match_beancount(scenario):
    """The M7 oracle deepens: Beancount re-books the exported reductions,
    and its per-account positions *at cost* — quantity, cost, lot date,
    label — must equal our surviving lots exactly. Scenarios without lots
    compare empty-to-empty for free."""
    from beancount import loader
    from beancount.core import data

    _name, led = scenario
    entries, errors, _options = loader.load_string(led.export_beancount_string())
    assert not errors, [str(error) for error in errors]

    oracle: dict[tuple, object] = {}
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            if posting.cost is None:
                continue
            key = (
                posting.account,
                posting.units.currency,
                posting.cost.number,
                posting.cost.date,
                posting.cost.label,
            )
            oracle[key] = oracle.get(key, 0) + posting.units.number
    oracle = {key: total for key, total in oracle.items() if total != 0}

    ours: dict[tuple, object] = {}
    for account in led.list_accounts():
        for position in led.inventory(account.path, include_children=False).positions():
            if position.cost is None:
                continue
            key = (
                account.path,
                position.units.commodity.symbol,
                position.cost.per_unit.to_decimal(),
                position.cost.date,
                position.cost.label,
            )
            ours[key] = ours.get(key, 0) + position.units.to_decimal()

    assert ours == oracle
