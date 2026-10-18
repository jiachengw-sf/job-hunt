"""SQLite store. Numbered SQL migrations applied in order; the version is PRAGMA user_version."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from jobhunt.paths import secure_file

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def _migrations() -> list[Migration]:
    root = resources.files("jobhunt.store").joinpath("migrations")
    found = []
    for entry in root.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    for i, m in enumerate(found, start=1):
        if m.version != i:
            raise RuntimeError(f"Migration numbering gap at {m.name}")
    return found


MIGRATIONS = _migrations()
SCHEMA_VERSION = MIGRATIONS[-1].version


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    secure_file(path)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def pending_migrations(conn: sqlite3.Connection) -> list[Migration]:
    v = current_version(conn)
    if v > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{v} is newer than this jobhunt (v{SCHEMA_VERSION}). Upgrade jobhunt."
        )
    return [m for m in MIGRATIONS if m.version > v]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations, each in its own transaction. Safe to re-run."""
    applied = []
    for m in pending_migrations(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would auto-commit; run statements one at a time inside the transaction
            for stmt in _split_sql(m.sql):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {m.version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(m.name)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements, keeping CREATE TRIGGER ... END; blocks whole."""
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if line.strip().startswith("--") and not buf.strip():
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements
