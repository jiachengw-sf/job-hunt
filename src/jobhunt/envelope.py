"""The JSON envelope every command prints: ``{ok, data, error, hints, page}``.

Output is capped at MAX_CHARS. Larger payloads go to a file (``--out`` or an automatic spill file)
and the envelope points at it, so an agent's context never receives an unbounded blob.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

MAX_CHARS = 8000


class ErrorInfo(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="What went wrong, and what to do about it.")
    details: dict[str, Any] | None = None


class Hints(BaseModel):
    next_actions: list[str] = Field(
        default_factory=list, description="Commands or steps the agent can take next."
    )
    user_actions: list[str] = Field(
        default_factory=list, description="Things only the human can do; relay them to the user."
    )


class Page(BaseModel):
    cursor: str | None = Field(None, description="Cursor of this page.")
    next_cursor: str | None = Field(None, description="Pass as --cursor to get the next page.")
    total: int | None = None
    out_file: str | None = Field(None, description="Full output was written to this file.")


class Envelope(BaseModel):
    ok: bool
    data: Any = None
    error: ErrorInfo | None = None
    hints: Hints = Field(default_factory=Hints)
    page: Page | None = None


def to_json(envelope: Envelope) -> str:
    return envelope.model_dump_json(exclude_none=False)


def spill_if_large(
    envelope: Envelope, *, out: Path | None, spill_dir: Path, command: str
) -> Envelope:
    """Write data to a file when asked (--out) or when the envelope would exceed MAX_CHARS."""
    if out is None and len(to_json(envelope)) <= MAX_CHARS:
        return envelope
    if out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        out = spill_dir / f"{command.replace(' ', '-')}-{stamp}.json"
        reason = f"Output exceeded {MAX_CHARS} characters."
    else:
        reason = "Output written to --out file."
    out.parent.mkdir(parents=True, exist_ok=True)
    full = json.dumps(envelope.data, indent=2, default=str)
    out.write_text(full, encoding="utf-8")
    page = (envelope.page or Page()).model_copy(update={"out_file": str(out)})
    hints = envelope.hints.model_copy(
        update={"next_actions": [f"Read {out} for the full data.", *envelope.hints.next_actions]}
    )
    return envelope.model_copy(
        update={
            "data": {"out_file": str(out), "chars": len(full), "note": reason},
            "page": page,
            "hints": hints,
        }
    )


# --- pagination -----------------------------------------------------------------------------


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        kind, value = raw.split(":", 1)
        if kind != "o" or int(value) < 0:
            raise ValueError
        return int(value)
    except (ValueError, UnicodeDecodeError) as e:
        from jobhunt.errors import ValidationFailed

        raise ValidationFailed(f"Invalid --cursor value: {cursor!r}") from e


def paginate(items: Sequence[Any], cursor: str | None, limit: int) -> tuple[list[Any], Page]:
    start = decode_cursor(cursor)
    chunk = list(items[start : start + limit])
    end = start + len(chunk)
    return chunk, Page(
        cursor=encode_cursor(start),
        next_cursor=encode_cursor(end) if end < len(items) else None,
        total=len(items),
    )


# --- human rendering ------------------------------------------------------------------------


def render_human_default(envelope: Envelope) -> str:
    lines: list[str] = []
    if envelope.error:
        lines.append(f"Error [{envelope.error.code}]: {envelope.error.message}")
    if envelope.data is not None:
        lines.append(yaml.safe_dump(envelope.data, sort_keys=False, allow_unicode=True).rstrip())
    if envelope.hints.user_actions:
        lines.append("\nYou need to:")
        lines += [f"  - {a}" for a in envelope.hints.user_actions]
    if envelope.hints.next_actions:
        lines.append("\nNext:")
        lines += [f"  - {a}" for a in envelope.hints.next_actions]
    if envelope.page and envelope.page.next_cursor:
        lines.append(f"\nMore results: --cursor {envelope.page.next_cursor}")
    return "\n".join(lines)
