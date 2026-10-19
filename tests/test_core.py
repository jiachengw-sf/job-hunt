"""Envelope paging/spill, locking, and the personal-data scanner."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from jobhunt.devtools.pii_scan import scan_file, scan_history, scan_repo, scan_text
from jobhunt.envelope import Envelope, decode_cursor, encode_cursor, paginate, spill_if_large
from jobhunt.errors import LockBusy, ValidationFailed
from jobhunt.locking import file_lock
from tests.conftest import REPO

# --- envelope -------------------------------------------------------------------------------


def test_paginate_walks_all_items():
    items = list(range(23))
    seen, cursor = [], None
    while True:
        chunk, page = paginate(items, cursor, 10)
        seen += chunk
        assert page.total == 23
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    assert seen == items


def test_bad_cursor_is_validation_error():
    with pytest.raises(ValidationFailed):
        decode_cursor("not-a-cursor!!")
    assert decode_cursor(encode_cursor(7)) == 7


def test_small_output_not_spilled(tmp_path: Path):
    env = Envelope(ok=True, data={"a": 1})
    assert spill_if_large(env, out=None, spill_dir=tmp_path, command="x") is env


def test_large_output_spilled(tmp_path: Path):
    env = Envelope(ok=True, data={"blob": "x" * 20000})
    out = spill_if_large(env, out=None, spill_dir=tmp_path, command="x")
    assert out.page.out_file and Path(out.page.out_file).read_text().count("x") >= 20000
    assert len(out.model_dump_json()) < 2000


# --- locking --------------------------------------------------------------------------------


def _hold_lock(path: str, ready, release) -> None:
    with file_lock(Path(path)):
        ready.set()
        release.wait(10)


def test_lock_excludes_other_process(tmp_path: Path):
    lock = tmp_path / ".lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(str(lock), ready, release))
    p.start()
    try:
        assert ready.wait(20)
        with pytest.raises(LockBusy), file_lock(lock, timeout=0.3):
            pass
    finally:
        release.set()
        p.join(10)
    with file_lock(lock, timeout=2):
        pass


# --- personal-data scanner ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "contact: jane.realperson@gmail.com",  # pii-scan: allow
        "phone: (415) 867-5309",  # pii-scan: allow
        "ssn 123-45-6789",  # pii-scan: allow
        "lives at 1600 Maple Avenue",  # pii-scan: allow
        "+44 20 7946 1234",  # pii-scan: allow
    ],
)
def test_scanner_flags_real_looking_data(line):
    text = line.split("  #")[0]
    assert scan_text(text, "f.txt"), f"not flagged: {text}"


@pytest.mark.parametrize(
    "line",
    [
        "a.b@example.com",
        "x@sub.example.com",
        "x@school.test",
        "git@github.com",
        "(312) 555-0101",
        "617-555-0199",
        "000-12-3456",
        "12 Example Street",
        "version 2026-10-18",
        "id 1234567890123",
    ],
)
def test_scanner_accepts_reserved_values(line):
    assert scan_text(line, "f.txt") == []


def test_scanner_allow_marker():
    assert scan_text("jane@gmail.com  # pii-scan: allow", "f") == []  # pii-scan: allow


def test_scanner_denylist():
    assert scan_text("Hello Quentin Zzyzx", "f", denylist=["quentin zzyzx"])
    assert scan_text("Hello there", "f", denylist=["quentin zzyzx"]) == []


def test_scanner_flags_resume_documents_outside_tests(tmp_path: Path):
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4")
    assert scan_file(tmp_path / "resume.pdf", tmp_path, [])[0].kind == "document"


def test_repo_has_no_personal_data():
    """The same check CI runs: the committed tree must be clean."""
    findings = scan_repo(REPO)
    assert findings == [], "\n".join(map(str, findings))


def test_history_scan_finds_data_a_later_commit_removed(tmp_path: Path):
    import subprocess

    def git(*a: str) -> None:
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    leaked = "jane" + "@" + "gmail.com"  # built at runtime so this file stays clean
    git("init", "-q")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "Dev")
    (tmp_path / "notes.txt").write_text(f"contact {leaked}\n")
    git("add", ".")
    git("commit", "-qm", "add notes")
    (tmp_path / "notes.txt").write_text("nothing here\n")
    git("commit", "-qam", f"scrub notes, ask {leaked}")
    assert scan_repo(tmp_path) == []  # the working tree is clean...
    found = {(f.path.split("@")[0], f.kind) for f in scan_history(tmp_path)}
    assert ("notes.txt", "email") in found  # ...but the old blob is still in history
    assert any(p.startswith("commit ") for p, _ in found)  # and so is the message
