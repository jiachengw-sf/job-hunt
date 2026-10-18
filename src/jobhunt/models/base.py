from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    """Base for user-file models: unknown keys are errors, not silently ignored."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
