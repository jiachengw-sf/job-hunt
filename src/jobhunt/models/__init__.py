"""Pydantic models for every user file. Unknown keys are rejected so typos surface as errors."""

from jobhunt.models.answers import Answers
from jobhunt.models.config import Config
from jobhunt.models.preferences import Preferences
from jobhunt.models.profile import Profile
from jobhunt.models.watchlist import Watchlist

__all__ = ["Answers", "Config", "Preferences", "Profile", "Watchlist"]
