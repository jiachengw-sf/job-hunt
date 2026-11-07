"""One adapter per supported ATS."""

from __future__ import annotations

from jobhunt.prefill.adapters.ashby import Ashby
from jobhunt.prefill.adapters.base import Adapter, FieldOutcome
from jobhunt.prefill.adapters.greenhouse import Greenhouse
from jobhunt.prefill.adapters.lever import Lever
from jobhunt.prefill.adapters.workday import Workday

ADAPTERS: dict[str, type[Adapter]] = {
    "greenhouse": Greenhouse,
    "lever": Lever,
    "ashby": Ashby,
    "workday": Workday,
}
# Read live in the browser after the human signs in, step by step (no form spec beforehand).
LIVE = frozenset({"workday"})


def adapter_for(ats: str, parse_wait_s: float = 20.0) -> Adapter:
    return ADAPTERS[ats](parse_wait_s=parse_wait_s)


__all__ = ["ADAPTERS", "LIVE", "Adapter", "FieldOutcome", "adapter_for"]
