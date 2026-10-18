"""Offline pages for the pre-fill browser (``jobhunt prefill --fixtures DIR``).

The browser still navigates to the real ATS URLs, so the submit guard sees exactly what it sees
live, but every request is answered from ``DIR`` and nothing reaches the network:

- GET ``https://host/a/b``: ``DIR/host/a/b``, then ``b.html``, ``b.json``, ``b/index.html``.
- Other methods: ``DIR/host/a/b/<op>.<METHOD>.json`` for a GraphQL ``?op=``, then
  ``DIR/host/a/b.<METHOD>.json`` (``index.<METHOD>.json`` for ``/``), then
  ``DIR/host/_default.<METHOD>.json``. Each holds ``{status, headers, content_type, json | body}``.
- Anything else is a 404; unknown hosts are aborted.

Every request that reaches the router is recorded (and appended to ``log_path`` as JSON lines), so
tests can prove which requests left the page. Requests the guard blocks never get here.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json", ".js": "text/javascript"}


class FixtureRouter:
    def __init__(self, root: Path, log_path: Path | None = None) -> None:
        self.root = root
        self.log_path = log_path
        self.requests: list[dict[str, str]] = []
        self._lock = threading.Lock()

    def install(self, context: Any) -> None:
        context.route("**/*", self._handle)

    def _record(self, method: str, url: str, status: int) -> None:
        entry = {"method": method, "url": url, "status": str(status)}
        with self._lock:
            self.requests.append(entry)
            if self.log_path:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")

    def _get_file(self, host: str, path: str) -> Path | None:
        base = self.root / host / path.strip("/")
        for p in (base, base.with_name(base.name + ".html"), base.with_name(base.name + ".json"),
                  base / "index.html"):  # fmt: skip
            if p.is_file() and self.root in p.resolve().parents:
                return p
        return None

    def _post_spec(
        self, host: str, path: str, method: str, op: str | None
    ) -> dict[str, Any] | None:
        stem = path.strip("/")
        cands = []
        if op:
            cands.append(self.root / host / stem / f"{op}.{method}.json")
        cands.append(self.root / host / f"{stem or 'index'}.{method}.json")
        cands.append(self.root / host / f"_default.{method}.json")
        for p in cands:
            if p.is_file() and self.root in p.resolve().parents:
                return json.loads(p.read_text(encoding="utf-8"))
        return None

    def _handle(self, route: Any) -> None:
        req = route.request
        parts = urlsplit(req.url)
        if parts.scheme not in ("http", "https"):
            route.fallback()
            return
        host, path, method = parts.hostname or "", parts.path, req.method.upper()
        if not (self.root / host).is_dir():
            self._record(method, req.url, 0)
            route.abort("internetdisconnected")
            return
        if method in ("GET", "HEAD"):
            f = self._get_file(host, path)
            if f is None:
                self._record(method, req.url, 404)
                route.fulfill(status=404, body="not found")
                return
            ctype = TYPES.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "text/plain"
            self._record(method, req.url, 200)
            route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
            return
        op = (parse_qs(parts.query).get("op") or [None])[0]
        spec = self._post_spec(host, path, method, op)
        if spec is None:
            self._record(method, req.url, 404)
            route.fulfill(status=404, body="not found")
            return
        status = int(spec.get("status", 200))
        self._record(method, req.url, status)
        if "json" in spec:
            body, ctype = json.dumps(spec["json"]), "application/json"
        else:
            body, ctype = spec.get("body", ""), spec.get("content_type", "text/plain")
        route.fulfill(
            status=status, headers=spec.get("headers") or {}, content_type=ctype, body=body
        )
