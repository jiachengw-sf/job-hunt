"""The status state machine from ``defaults/states.yml``."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import yaml

from jobhunt.errors import NeedsUser, ValidationFailed
from jobhunt.paths import package_file


@dataclass(frozen=True)
class State:
    name: str
    rank: int
    terminal: bool = False
    needs_reason: bool = False


@dataclass(frozen=True)
class Machine:
    states: dict[str, State]
    initial: str
    skip_reasons: tuple[str, ...]

    def get(self, name: str) -> State:
        """Case-insensitive lookup; raises ValidationFailed for an unknown status."""
        for s in self.states.values():
            if s.name.lower() == name.strip().lower():
                return s
        raise ValidationFailed(f"Unknown status {name!r}.", details={"statuses": list(self.states)})

    def is_valid(self, name: str | None) -> bool:
        return bool(name) and any(s.lower() == name.strip().lower() for s in self.states)  # type: ignore[union-attr]

    def agent_can_move(self, current: str, new: str) -> bool:
        """The agent only moves forward, and never out of a terminal state."""
        cur, nxt = self.get(current), self.get(new)
        return not cur.terminal and nxt.rank > cur.rank

    def user_move(self, new: str, reason: str | None) -> State:
        """Any status the user picks is allowed; Skipped needs a reason."""
        nxt = self.get(new)
        if nxt.needs_reason:
            if not reason:
                raise NeedsUser(
                    f"{nxt.name} needs a reason.",
                    details={"reasons": list(self.skip_reasons)},
                    user_actions=[f"Why skip it? One of: {', '.join(self.skip_reasons)}."],
                    next_actions=[f"Re-run with --reason <{'|'.join(self.skip_reasons)}>."],
                )
            if reason not in self.skip_reasons:
                raise ValidationFailed(
                    f"Skip reason must be one of: {', '.join(self.skip_reasons)}."
                )
        return nxt


@cache
def machine() -> Machine:
    data = yaml.safe_load(package_file("defaults", "states.yml").read_text(encoding="utf-8"))
    states = {s["name"]: State(**s) for s in data["states"]}
    return Machine(states, data["initial"], tuple(data.get("skip_reasons") or ()))
