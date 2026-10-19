"""The local onboarding form server.

``jobhunt setup form`` starts this module as a detached process and returns at once with the
URL. The server binds 127.0.0.1 only, requires the one-time token from the URL on every request,
checks Host and Origin (no other site can post to it), writes preferences.yaml and answers.yaml
on Save, and exits after ``IDLE_MINUTES`` without requests. Its state is a JSON file that
``jobhunt setup form --status`` reads.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobhunt.config import read_yaml
from jobhunt.models import Profile
from jobhunt.onboarding import form as F
from jobhunt.paths import Paths, get_paths, secure_file

IDLE_MINUTES = 60
MAX_BODY = 1_000_000


def state_path(paths: Paths) -> Path:
    return paths.onboarding / "form-state.json"


def contact_path(paths: Paths) -> Path:
    """Contact details read from the resume: pre-fill only, never shown to the model."""
    return paths.onboarding / "contact.json"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_state(paths: Paths) -> dict[str, Any] | None:
    p = state_path(paths)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def write_state(paths: Paths, state: dict[str, Any]) -> None:
    p = state_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    secure_file(tmp)
    tmp.replace(p)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class FormApp:
    """Request handling, independent of the socket (tests drive it directly)."""

    def __init__(self, paths: Paths, token: str) -> None:
        self.paths = paths
        self.token = token
        self.lock = threading.Lock()
        self.last_request = time.monotonic()
        self.saves = 0

    def _existing(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        prefs = read_yaml(self.paths.preferences) if self.paths.preferences.exists() else None
        answers = read_yaml(self.paths.answers) if self.paths.answers.exists() else None
        return prefs, answers

    def values(self) -> dict[str, Any]:
        prefs, answers = self._existing()
        profile = None
        if self.paths.profile.exists():
            with contextlib.suppress(Exception):
                profile = Profile.model_validate(read_yaml(self.paths.profile))
        contact = None
        cp = contact_path(self.paths)
        if cp.exists():
            contact = json.loads(cp.read_text(encoding="utf-8"))
        return F.prefill(prefs, answers, profile=profile, contact=contact)

    def page(self, *, errors: list[str] | None = None, saved: list[str] | None = None,
             vals: dict[str, Any] | None = None) -> str:  # fmt: skip
        return F.render(
            vals if vals is not None else self.values(),
            token=self.token,
            new_questions=F.load_new_questions(self.paths.new_questions),
            errors=errors,
            saved=saved,
        )

    def save(self, body: str) -> tuple[bool, str]:
        form = parse_qs(body, keep_blank_values=True)
        if not hmac.compare_digest(form.get("token", [""])[0], self.token):
            return False, "bad token"
        prefs_old, answers_old = self._existing()
        nq = F.load_new_questions(self.paths.new_questions)
        prefs, answers, errors = F.parse(
            form, existing_prefs=prefs_old, existing_answers=answers_old, new_questions=nq
        )
        errors += F.validate(prefs, answers)
        if errors:
            return False, self.page(errors=errors, vals=F.posted_values(form))
        with self.lock:
            self.paths.home.mkdir(parents=True, exist_ok=True)
            F.write_yaml(self.paths.preferences, prefs, "Written by the jobhunt setup form.")
            F.write_yaml(
                self.paths.answers,
                answers,
                "Written by the jobhunt setup form. Sensitive: never share.",
            )
            self.saves += 1
            st = read_state(self.paths) or {}
            st.update(status="saved", saved_at=now_iso(), saves=self.saves)
            write_state(self.paths, st)
        return True, ""


def _handler(app: FormApp) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        server_version = "jobhunt-form"

        def log_message(self, fmt: str, *args: Any) -> None:  # requests go to the log quietly
            sys.stderr.write("form: " + (fmt % args) + "\n")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            # same-origin, not no-referrer: with no-referrer Chrome posts "Origin: null"
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            app.last_request = time.monotonic()
            if not self._host_ok():
                return self._send(403, "Forbidden", "text/plain")
            if urlsplit(self.path).path != "/":
                return self._send(404, "Not found", "text/plain")
            q = parse_qs(urlsplit(self.path).query)
            if not hmac.compare_digest(q.get("t", [""])[0], app.token):
                return self._send(
                    403, "This link has expired. Run `jobhunt setup form` again.", "text/plain"
                )
            saved = ["preferences.yaml", "answers.yaml"] if q.get("saved") else None
            self._send(200, app.page(saved=saved))

        def do_POST(self) -> None:
            app.last_request = time.monotonic()
            origin = self.headers.get("Origin")
            if not self._host_ok() or (
                origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost")
            ):
                return self._send(403, "Forbidden", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, "Too large", "text/plain")
            body = self.rfile.read(n).decode("utf-8", "replace")
            ok, page = app.save(body)
            if page == "bad token":
                return self._send(403, "Forbidden", "text/plain")
            if ok:  # post/redirect/get: a reload doesn't post again
                self.send_response(303)
                self.send_header("Location", f"/?t={app.token}&saved=1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            self._send(400, page)

    return H


def make_server(paths: Paths, token: str, port: int = 0) -> ThreadingHTTPServer:
    """Bind 127.0.0.1 and record the URL in the state file (not yet serving)."""
    app = FormApp(paths, token)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(app))
    httpd.app = app  # type: ignore[attr-defined]
    real_port = httpd.server_address[1]
    st = read_state(paths) or {}
    write_state(
        paths,
        {
            **{k: st[k] for k in ("saved_at", "saves") if k in st},
            "pid": os.getpid(),
            "port": real_port,
            "url": f"http://127.0.0.1:{real_port}/?t={token}",
            "status": "saved" if st.get("saved_at") else "waiting",
            "started_at": now_iso(),
        },
    )
    return httpd


def serve(paths: Paths, token: str, port: int = 0, idle_minutes: float = IDLE_MINUTES) -> None:
    httpd = make_server(paths, token, port)
    app: FormApp = httpd.app  # type: ignore[attr-defined]

    def watchdog() -> None:
        while True:
            time.sleep(5)
            if time.monotonic() - app.last_request > idle_minutes * 60:
                httpd.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown).start())
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        st = read_state(paths) or {}
        if st.get("pid") == os.getpid():
            st["status"] = "saved_and_stopped" if st.get("saved_at") else "stopped"
            st["stopped_at"] = now_iso()
            write_state(paths, st)


def launch(paths: Paths, *, port: int = 0, wait_s: float = 20.0) -> dict[str, Any]:
    """Start the server detached and return its state once it is listening."""
    st = read_state(paths)
    if st and st.get("status") in ("waiting", "saved") and pid_alive(st.get("pid")):
        return {**st, "reused": True}
    token = secrets.token_urlsafe(24)
    log_dir = paths.logs
    log_dir.mkdir(parents=True, exist_ok=True)
    paths.onboarding.mkdir(parents=True, exist_ok=True)
    if st:
        write_state(paths, {k: st[k] for k in ("saved_at", "saves") if k in st})
    else:
        state_path(paths).unlink(missing_ok=True)
    env = {**os.environ, "JOBHUNT_HOME": str(paths.home), "JOBHUNT_FORM_TOKEN": token}
    log = open(log_dir / "form.log", "ab")  # noqa: SIM115 - handed to the child process
    kwargs: dict[str, Any] = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-m", "jobhunt.onboarding.server", "--port", str(port)], **kwargs
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        cur = read_state(paths)
        # Match on the one-time token, not the pid: some Python launchers re-exec themselves
        # (e.g. macOS framework builds), so the listening process may have another pid.
        if cur and token in (cur.get("url") or ""):
            return {**cur, "reused": False}
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    log.close()
    code = proc.poll()
    why = f"it exited with code {code}" if code is not None else f"not listening after {wait_s:g}s"
    raise RuntimeError(
        f"The form server didn't start ({why}); see {log_dir / 'form.log'}. "
        f"Last lines: {_log_tail(log_dir / 'form.log')}"
    )


def _log_tail(path: Path, lines: int = 5) -> str:
    try:
        tail = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
    except OSError:
        return "(no log)"
    return " | ".join(tail) or "(empty log)"


def stop(paths: Paths) -> dict[str, Any] | None:
    st = read_state(paths)
    if not st:
        return None
    if pid_alive(st.get("pid")):
        with contextlib.suppress(OSError):
            os.kill(int(st["pid"]), signal.SIGTERM)
        for _ in range(30):
            if not pid_alive(st.get("pid")):
                break
            time.sleep(0.1)
    st = read_state(paths) or st
    if st.get("status") in ("waiting", "saved"):
        st["status"] = "saved_and_stopped" if st.get("saved_at") else "stopped"
        write_state(paths, st)
    return st


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--idle-minutes", type=float, default=IDLE_MINUTES)
    args = ap.parse_args()
    token = os.environ.get("JOBHUNT_FORM_TOKEN") or secrets.token_urlsafe(24)
    serve(get_paths(), token, args.port, args.idle_minutes)


if __name__ == "__main__":
    main()
