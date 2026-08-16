"""Storage plumbing: pragmas, migrations, unit of work."""

import sqlite3

import pytest

from ledger.storage.db import connect, migrate, unit_of_work


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(conn):
    migrate(conn)
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ]
    assert versions == [1, 2, 3, 4, 5]
    applied = len(versions)
    migrate(conn)  # no error, no re-application
    count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == applied


def test_migrations_create_tables(conn):
    migrate(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "commodities",
        "accounts",
        "transactions",
        "postings",
        "ledger_options",
        "schema_migrations",
        "balance_assertions",
        "pads",
        "tags",
        "transaction_tags",
        "links",
        "notes",
        "documents",
        "events",
    } <= tables


def test_unit_of_work_commits(conn):
    migrate(conn)
    with unit_of_work(conn):
        conn.execute(
            "INSERT INTO ledger_options (key, value) VALUES (?, ?)",
            ("operating_currency", "USD"),
        )
    assert conn.execute("SELECT COUNT(*) FROM ledger_options").fetchone()[0] == 1


def test_unit_of_work_rolls_back_on_error(conn):
    migrate(conn)
    with pytest.raises(RuntimeError):
        with unit_of_work(conn):
            conn.execute(
                "INSERT INTO ledger_options (key, value) VALUES (?, ?)",
                ("operating_currency", "USD"),
            )
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM ledger_options").fetchone()[0] == 0


def test_unit_of_work_joins_callers_transaction(conn):
    """An embedding application wrapping a ledger write in its own
    transaction keeps ownership of the commit (design §13)."""
    migrate(conn)
    conn.execute("BEGIN")
    with unit_of_work(conn):
        conn.execute(
            "INSERT INTO ledger_options (key, value) VALUES (?, ?)",
            ("operating_currency", "USD"),
        )
    assert conn.in_transaction  # unit_of_work did not commit
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT COUNT(*) FROM ledger_options").fetchone()[0] == 0


def test_wal_survives_reconnect(tmp_path):
    path = tmp_path / "test.db"
    first = connect(path)
    first.close()
    second = sqlite3.connect(path)
    try:
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        second.close()
