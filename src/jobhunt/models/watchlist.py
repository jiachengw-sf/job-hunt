"""watchlist.yaml: companies to watch directly, by careers page or ATS board."""

from __future__ import annotations

from pydantic import Field

from jobhunt.models.base import Strict


class WatchedCompany(Strict):
    name: str
    careers_url: str | None = Field(
        None, description="Any careers or board URL; the ATS resolver reads it."
    )
    ats: str | None = Field(
        None, description="greenhouse | lever | ashby | ... (optional; detected)"
    )
    board_token: str | None = None
    notes: str | None = None


class Watchlist(Strict):
    version: int = 1
    companies: list[WatchedCompany] = Field(default_factory=list)
