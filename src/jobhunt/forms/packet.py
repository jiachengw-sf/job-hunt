"""The hand-off packet: a local page for applying by hand (Manual jobs, or anyone who prefers it).

It holds the apply link, the fit summary, every known answer ready to copy with its character
count (and the field's limit when the form has one), the essay prompts, and the fields only the
user should touch. It is written to ~/.jobhunt/work/packets/ (owner-only) and never leaves the
machine. All posting and form text is escaped: it is untrusted data.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PacketAnswer:
    label: str
    value: str | None
    note: str | None = None
    max_length: int | None = None
    required: bool = False
    human: list[str] = field(default_factory=list)


@dataclass
class Packet:
    job: dict[str, Any]
    fit: dict[str, Any] | None
    cls: str | None
    reasons: list[str]
    form_answers: list[PacketAnswer]
    common_answers: list[PacketAnswer]
    essays: list[PacketAnswer]
    captcha_at_submit: bool = False
    requirements: list[dict[str, Any]] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _safe_url(u: str | None) -> str | None:
    return u if u and u.lower().startswith(("https://", "http://")) else None


def _answer_row(a: PacketAnswer, i: int) -> str:
    count = len(a.value) if a.value else 0
    limit = f" / {a.max_length}" if a.max_length else ""
    over = a.max_length is not None and count > a.max_length
    flags = "".join(f'<div class="human">{_e(h)}</div>' for h in a.human)
    if a.value:
        val = (
            f'<pre id="v{i}">{_e(a.value)}</pre>'
            f"<button onclick=\"copy('v{i}', this)\">Copy</button>"
            f'<span class="count{" over" if over else ""}">{count}{limit} chars</span>'
        )
    else:
        val = f'<span class="missing">{_e(a.note or "no saved answer")}</span>'
    req = ' <span class="req">required</span>' if a.required else ""
    return f'<div class="row"><div class="label">{_e(a.label)}{req}</div>{val}{flags}</div>'


def render(p: Packet) -> str:
    j = p.job
    url = _safe_url(j.get("url"))
    link = (
        f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(url)}</a>'
        if url
        else "—"
    )
    fit = p.fit or {}
    band = fit.get("band") or "unknown"
    if fit.get("provisional"):
        band += " (provisional)"
    reqs = "".join(
        f"<li><b>{_e(r.get('status') or 'unassessed')}</b> · {_e(r.get('importance'))} · "
        f"{_e(r.get('text'))}</li>"
        for r in p.requirements[:15]
    )
    gates = "".join(
        f"<li>{_e(g.get('gate'))}: {_e(g.get('result'))} — {_e(g.get('reason') or '')}</li>"
        for g in p.gates
        if g.get("result") != "pass"
    )
    form_rows = "".join(_answer_row(a, i) for i, a in enumerate(p.form_answers))
    common_rows = "".join(
        _answer_row(a, 1000 + i) for i, a in enumerate(p.common_answers) if a.value
    )
    essay_rows = "".join(
        _answer_row(a, 2000 + i)
        if a.value
        else f'<div class="row"><div class="label">{_e(a.label)}</div>'
        f'<div class="note">{_e(a.note or "")}'
        f"{' · limit ' + str(a.max_length) + ' chars' if a.max_length else ''}</div>"
        f'<span class="missing">No approved draft yet: write it with the essay skill.</span></div>'
        for i, a in enumerate(p.essays)
    )
    captcha = (
        "<p class='note'>This form has a CAPTCHA on Submit: that part is yours.</p>"
        if p.captcha_at_submit
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>{_e(j.get("company"))}: {_e(j.get("title"))}</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--accent:#1f5f8b;--warn:#a1460b;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161614;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--accent:#7fb6dd;--warn:#f0a35e;--card:#1f1f1c}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:820px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.meta,.note{{color:var(--muted);font-size:13px}}a{{color:var(--accent);word-break:break-all}}
.row{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}}
.label{{font-weight:600;margin-bottom:4px}}pre{{white-space:pre-wrap;margin:4px 0;font:14px/1.45 ui-monospace,monospace}}
button{{font:inherit;font-size:13px;padding:2px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer;margin-right:8px}}
.count{{color:var(--muted);font-size:12px}}.over{{color:var(--warn);font-weight:600}}.missing{{color:var(--muted);font-style:italic}}
.human{{color:var(--warn);font-size:13px}}.req{{color:var(--warn);font-size:12px;font-weight:500}}.cls{{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid var(--line);font-size:13px}}
</style></head><body><main>
<h1>{_e(j.get("company"))}: {_e(j.get("title"))}</h1>
<div class="meta">{_e(", ".join(j.get("locations") or []))} · <span class="cls">{_e(p.cls or "not inspected")}</span> {_e("; ".join(p.reasons))}</div>
<p>Apply here: {link}</p>{captcha}
<p class="note">You fill and submit this application yourself. jobhunt never submits.</p>
<h2>Fit</h2><p>Band: <b>{_e(band)}</b> · priority {_e(fit.get("priority"))}</p>
{f"<ul>{gates}</ul>" if gates else ""}{f"<ul>{reqs}</ul>" if reqs else "<p class='note'>No requirements extracted yet.</p>"}
<h2>This form's questions</h2>{form_rows or "<p class='note'>The form wasn't read (unsupported ATS or not inspected). Use the common answers below.</p>"}
<h2>Essays and free text</h2>{essay_rows or "<p class='note'>None required.</p>"}
<h2>Common answers</h2>{common_rows or "<p class='note'>No saved answers yet.</p>"}
</main><script>
function copy(id,btn){{const t=document.getElementById(id).textContent;navigator.clipboard.writeText(t).then(()=>{{btn.textContent='Copied';setTimeout(()=>btn.textContent='Copy',1200)}})}}
</script></body></html>
"""
