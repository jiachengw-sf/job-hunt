"""profile.yaml edits: confirm a draft, and set one value by dotted path.

Paths use dots and list indices: ``skills.languages``, ``experience[0].title``,
``projects[1].bullets[2]``. ``--append`` adds to a list. Every write is validated against the
Profile model first.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import yaml
from pydantic import ValidationError

from jobhunt.errors import ValidationFailed
from jobhunt.models import Profile

_PART = re.compile(r"([A-Za-z_][\w-]*)|\[(\d+)\]")


def parse_path(path: str) -> list[str | int]:
    out: list[str | int] = []
    for seg in path.split("."):
        pos = 0
        if not seg:
            raise ValidationFailed(f"Bad path {path!r}: empty segment.")
        while pos < len(seg):
            m = _PART.match(seg, pos)
            if not m:
                raise ValidationFailed(f"Bad path {path!r} near {seg[pos:]!r}.")
            out.append(m.group(1) if m.group(1) else int(m.group(2)))
            pos = m.end()
    return out


def parse_value(raw: str) -> Any:
    """Profile values are text or lists of text: ``[a, b]`` is a list, ``null`` clears the
    value, anything else is the text as written (``2027-05`` stays a string)."""
    t = raw.strip()
    if t in ("null", "~"):
        return None
    if t.startswith("[") and t.endswith("]"):
        try:
            v = yaml.safe_load(t)
        except yaml.YAMLError:
            return raw
        return [str(x) for x in v]
    return raw


def set_value(
    data: dict[str, Any], path: str, value: Any, *, append: bool = False
) -> dict[str, Any]:
    out = copy.deepcopy(data)
    parts = parse_path(path)
    cur: Any = out
    for i, part in enumerate(parts[:-1]):
        nxt_is_index = isinstance(parts[i + 1], int)
        if isinstance(part, int):
            if not isinstance(cur, list) or part >= len(cur):
                raise ValidationFailed(f"{path}: index {part} is out of range.")
            cur = cur[part]
        else:
            if not isinstance(cur, dict):
                raise ValidationFailed(f"{path}: {part!r} is not inside a mapping.")
            if cur.get(part) is None:
                cur[part] = [] if nxt_is_index else {}
            cur = cur[part]
    last = parts[-1]
    if append:
        target = cur[last] if isinstance(last, int) else cur.setdefault(last, [])
        if target is None:
            target = cur[last] = []
        if not isinstance(target, list):
            raise ValidationFailed(f"{path} is not a list; drop --append.")
        target.extend(value if isinstance(value, list) else [value])
    elif isinstance(last, int):
        if not isinstance(cur, list) or last >= len(cur):
            raise ValidationFailed(f"{path}: index {last} is out of range.")
        cur[last] = value
    else:
        if not isinstance(cur, dict):
            raise ValidationFailed(f"{path}: not inside a mapping.")
        cur[last] = value
    return out


def check(data: dict[str, Any], where: str) -> Profile:
    try:
        return Profile.model_validate(data)
    except ValidationError as e:
        msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ValidationFailed(
            f"{where} isn't a valid profile: " + "; ".join(msgs[:8]),
            details={"errors": msgs},
        ) from e
