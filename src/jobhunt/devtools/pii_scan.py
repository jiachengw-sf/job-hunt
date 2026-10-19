"""Fail when real-looking personal data is committed. Run in CI and as a test.

    python -m jobhunt.devtools.pii_scan [PATH ...]     # default: every git-tracked file
    python -m jobhunt.devtools.pii_scan --history      # every blob and commit message ever

Synthetic data must use reserved values: example.com / .example / .test / .invalid emails,
555-01xx phone numbers, obviously fake addresses. A line can opt out with ``pii-scan: allow``.

Add your own real name, email or school to a local denylist (one term per line) so the scan also
catches them: ``$JOBHUNT_PII_DENYLIST``, ``./.pii-denylist`` (gitignored) or
``~/.jobhunt/pii-denylist.txt``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARK = "pii-scan: allow"

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}
SAFE_EMAIL_TLDS = {"example", "test", "invalid", "localhost"}
SAFE_EMAIL_EXACT = {"git@github.com"}

# North American numbers. 555-0100..555-0199 is reserved for fiction.
NANP_RE = re.compile(
    r"(?<![\d-])(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-]?(\d{3})[ .-](\d{4})(?![\d-])"
)
INTL_RE = re.compile(r"(?<![\w+])\+(?!1\b)(\d{1,3})[ .-]?\d{2,4}[ .-]?\d{3,4}[ .-]?\d{3,4}\b")
SSN_RE = re.compile(r"(?<![\d-])(\d{3})-(\d{2})-(\d{4})(?![\d-])")
STREET_RE = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way)\b\.?"
)
FAKE_WORDS = re.compile(r"\b(?:Example|Fake|Test|Sample|Fixture)\b", re.IGNORECASE)

RESUME_EXTS = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".pages"}
BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".db",
    ".sqlite",
    ".zip",
    ".gz",
    ".whl",
}
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.text}"


def _email_ok(addr: str, domain: str) -> bool:
    d = domain.lower()
    if addr.lower() in SAFE_EMAIL_EXACT or d in SAFE_EMAIL_DOMAINS:
        return True
    return d.rsplit(".", 1)[-1] in SAFE_EMAIL_TLDS or d.endswith(".example.com")


def _phone_ok(exchange: str, line: str) -> bool:
    return exchange == "555" and line.startswith("01")


def _ssn_ok(area: str, group: str, serial: str) -> bool:
    # Never-issued values: area 000, 666 or 9xx; group 00; serial 0000
    return area in {"000", "666"} or area.startswith("9") or group == "00" or serial == "0000"


def scan_text(text: str, path: str, denylist: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    deny = [d.lower() for d in (denylist or []) if d.strip()]
    for n, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARK in line:
            continue
        for m in EMAIL_RE.finditer(line):
            if not _email_ok(m.group(0), m.group(1)):
                findings.append(Finding(path, n, "email", m.group(0)))
        for m in NANP_RE.finditer(line):
            if not _phone_ok(m.group(2), m.group(3)):
                findings.append(Finding(path, n, "phone", m.group(0).strip()))
        for m in INTL_RE.finditer(line):
            if "555" not in m.group(0):
                findings.append(Finding(path, n, "phone", m.group(0)))
        for m in SSN_RE.finditer(line):
            if not _ssn_ok(*m.groups()):
                findings.append(Finding(path, n, "ssn", m.group(0)))
        for m in STREET_RE.finditer(line):
            if not FAKE_WORDS.search(m.group(0)):
                findings.append(Finding(path, n, "street_address", m.group(0)))
        low = line.lower()
        for term in deny:
            if term in low:
                findings.append(Finding(path, n, "denylisted", "(term from local denylist)"))
    return findings


def _scan_blob(
    rel: str, data: bytes, denylist: list[str], label: str | None = None
) -> list[Finding]:
    ext = Path(rel).suffix.lower()
    if ext in RESUME_EXTS and not rel.startswith("tests/"):
        return [
            Finding(
                label or rel,
                0,
                "document",
                "resume-type document outside tests/ (keep resumes in ~/.jobhunt/resume/)",
            )
        ]
    if ext in BINARY_EXTS or ext in RESUME_EXTS:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return scan_text(text, label or rel, denylist)


def scan_file(path: Path, root: Path, denylist: list[str]) -> list[Finding]:
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    try:
        data = path.read_bytes()
    except OSError:
        return []
    return _scan_blob(rel, data, denylist)


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, input=stdin, capture_output=True, check=True
    ).stdout


def scan_history(root: Path) -> list[Finding]:
    """Every blob reachable from any ref, under the first path it was seen at, plus every commit
    message. A file deleted or rewritten later still counts: it stays in a public clone."""
    denylist = load_denylist(root)
    paths: dict[str, str] = {}
    for line in _git(root, "rev-list", "--objects", "--all").decode().splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in paths:
            paths[sha] = path
    findings: list[Finding] = []
    if paths:
        out = _git(root, "cat-file", "--batch", stdin="\n".join(paths).encode() + b"\n")
        i = 0
        while i < len(out):
            nl = out.index(b"\n", i)
            sha, kind, size = out[i:nl].decode().split()
            body = out[nl + 1 : nl + 1 + int(size)]
            i = nl + 1 + int(size) + 1
            if kind == "blob":
                rel = paths[sha]
                findings += _scan_blob(rel, body, denylist, f"{rel}@{sha[:8]}")
    log = _git(root, "log", "--all", "--format=%H%x00%B%x01").decode("utf-8", "replace")
    for entry in log.split("\x01"):
        sha, _, msg = entry.strip("\n").partition("\x00")
        if sha:
            findings += scan_text(msg, f"commit {sha[:8]}", denylist)
    return findings


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        return [root / p for p in out.decode().split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in root.rglob("*") if p.is_file() and not (set(p.parts) & SKIP_PARTS)]


def load_denylist(root: Path) -> list[str]:
    candidates = [
        os.environ.get("JOBHUNT_PII_DENYLIST"),
        root / ".pii-denylist",
        Path.home() / ".jobhunt" / "pii-denylist.txt",
    ]
    terms: list[str] = []
    for c in candidates:
        if c and Path(c).is_file():
            terms += [
                t.strip()
                for t in Path(c).read_text(encoding="utf-8").splitlines()
                if t.strip() and not t.startswith("#")
            ]
    return terms


def scan_repo(root: Path, paths: list[Path] | None = None) -> list[Finding]:
    denylist = load_denylist(root)
    findings: list[Finding] = []
    for p in paths or tracked_files(root):
        if p.is_file():
            findings += scan_file(p.resolve(), root.resolve(), denylist)
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path.cwd()
    if "--history" in args:
        findings = scan_history(root)
    else:
        findings = scan_repo(root, [Path(a) for a in args] or None)
    for f in findings:
        print(f, file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} possible personal-data finding(s). Use reserved test values "
            f"(see tests/personas/README.md) or mark a deliberate line with '{ALLOW_MARK}'.",
            file=sys.stderr,
        )
        return 1
    scope = "git history" if "--history" in args else "files"
    print(f"personal-data scan ({scope}): clean", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
