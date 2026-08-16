"""The `ledger validate` CLI (plan §4.3). Exit codes: 0 clean, 1
findings, 2 usage error."""

from scenarios import build_month_of_spending
from test_append_only import drop_append_only_triggers

from ledger.api import Ledger
from ledger.cli import main


def test_validate_clean_ledger_exits_zero(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    with Ledger.open(path) as led:
        build_month_of_spending(led)
    assert main(["validate", str(path)]) == 0
    assert "ok: all checks passed" in capsys.readouterr().out


def test_validate_corrupt_ledger_exits_one(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    with Ledger.open(path) as led:
        build_month_of_spending(led)
        drop_append_only_triggers(led._conn)
        led._conn.execute(
            "UPDATE postings SET units = -units, weight = -weight"
            " WHERE id = (SELECT MIN(id) FROM postings)"
        )
    assert main(["validate", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[transaction-balance]" in out


def test_missing_database_exits_two(tmp_path, capsys):
    assert main(["validate", str(tmp_path / "nope.db")]) == 2
    assert "no such database" in capsys.readouterr().err
