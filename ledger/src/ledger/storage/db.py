"""SQLite connection, pragmas, migrations, unit of work."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a connection with the ledger's required pragmas.

    The connection runs in autocommit mode; write grouping is done
    explicitly through unit_of_work().
    """
    conn = sqlite3.connect(path)
    conn.autocommit = True
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _iter_migrations() -> list[tuple[int, str, str]]:
    root = resources.files("ledger.storage") / "migrations"
    migrations = []
    for entry in root.iterdir():
        if entry.name.endswith(".sql"):
            version = int(entry.name.split("_", 1)[0])
            migrations.append((version, entry.name, entry.read_text()))
    return sorted(migrations)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply any unapplied migrations. Idempotent.

    Also ensures foreign_keys is on for borrowed connections (a no-op if
    the caller is mid-transaction, where SQLite ignores the pragma).
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, _name, sql in _iter_migrations():
        if version in applied:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )


@contextmanager
def unit_of_work(conn: sqlite3.Connection) -> Iterator[None]:
    """Commit the enclosed writes atomically, or roll them all back.

    If the connection is already inside a transaction (an embedding
    application wrapping a ledger write with its own), the existing
    transaction is joined and the caller keeps ownership of the commit.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
