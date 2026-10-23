"""profile.yaml edits: confirm a draft, and set one value by dotted path.

Paths use dots and list indices: ``skills.languages``, ``experience[0].title``,
``projects[1].bullets[2]``. ``--append`` adds to a list. Every write is validated against the
Profile model first.
"""

from __future__ import annotations

import copy
import json
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
    """Profile values are text, lists or objects: JSON (``{"name": ...}``,
    ``[{"school": ...}]``) or a flow list (``[a, b]``) is parsed, ``null`` clears the value,
    anything else is the text as written (``2027-05`` stays a string). Scalars inside are
    kept as text, so the Profile model reads them the same way as YAML."""
    t = raw.strip()
    if t in ("null", "~"):
        return None
    if (t.startswith("[") and t.endswith("]")) or (t.startswith("{") and t.endswith("}")):
        try:
            v = json.loads(t)
        except ValueError:
            try:
                v = yaml.safe_load(t)
            except yaml.YAMLError:
                return raw
        return _texts(v)
    return raw


def _texts(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _texts(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_texts(x) for x in v]
    if v is None or isinstance(v, bool):
        return v
    return str(v)


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
        if value is None:
            cur.pop(last, None)  # null removes the key (its default applies)
        else:
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
