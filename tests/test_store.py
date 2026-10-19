from __future__ import annotations

import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from jobhunt.store import SCHEMA_VERSION, connect, current_version, migrate, pending_migrations
from jobhunt.store.db import _split_sql

NOW = "2026-10-18T12:00:00Z"


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "jobhunt.db")
    migrate(c)
    yield c
    c.close()


def test_migrate_is_idempotent(tmp_path: Path):
    c = connect(tmp_path / "x.db")
    assert migrate(c) == [
        "0001_init.sql",
        "0002_discovery.sql",
        "0003_fit.sql",
        "0004_forms.sql",
        "0005_ingest.sql",
        "0006_research.sql",
        "0007_drafts.sql",
        "0008_notifications.sql",
    ]
    assert migrate(c) == []
    assert current_version(c) == SCHEMA_VERSION
    assert pending_migrations(c) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_db_is_owner_only(tmp_path: Path):
    p = tmp_path / "x.db"
    connect(p).close()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def _add_job(conn, key="greenhouse:acme:123") -> int:
    cur = conn.execute(
        "INSERT INTO jobs (dedupe_key, ats, company, title, first_seen, last_seen) "
        "VALUES (?, 'greenhouse', 'Acme', 'Intern', ?, ?)",
        (key, NOW, NOW),
    )
    return cur.lastrowid


def test_dedupe_key_unique(conn):
    _add_job(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_job(conn)


def test_posted_at_nullable_first_seen_required(conn):
    jid = _add_job(conn)
    row = conn.execute("SELECT posted_at, first_seen FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["posted_at"] is None and row["first_seen"] == NOW
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (dedupe_key, company, title, last_seen) VALUES ('k','a','b',?)",
            (NOW,),
        )


def test_events_are_append_only(conn):
    jid = _add_job(conn)
    conn.execute("INSERT INTO events (job_id, kind, at) VALUES (?, 'appeared', ?)", (jid, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET kind='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_check_constraints(conn):
    jid = _add_job(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gate_results (job_id, gate, result, at) VALUES (?, 'yoe', 'maybe', ?)",
            (jid, NOW),
        )
    conn.execute(
        "INSERT INTO gate_results (job_id, gate, result, at) "
        "VALUES (?, 'yoe', 'pass_unverified', ?)",
        (jid, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fits (job_id, band, computed_at) VALUES (?, 'amazing', ?)", (jid, NOW)
        )


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_urls (job_id, url, source_id, first_seen) VALUES (999, 'u', 's', ?)",
            (NOW,),
        )


def test_newer_db_refuses(tmp_path: Path):
    c = connect(tmp_path / "x.db")
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError, match="newer"):
        migrate(c)


def test_split_sql_keeps_triggers_whole():
    sql = "CREATE TABLE a (x);\nCREATE TRIGGER t BEFORE DELETE ON a\nBEGIN SELECT 1; END;\n"
    parts = _split_sql(sql)
    assert len(parts) == 2 and parts[1].endswith("END;")
