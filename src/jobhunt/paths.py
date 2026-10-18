"""The one place that knows where files live.

User layer: ``~/.jobhunt/`` (override with ``$JOBHUNT_HOME``). Holds personal data and is never
touched by upgrades. System layer: the installed package (``jobhunt/defaults``).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

ENV_HOME = "JOBHUNT_HOME"
DIR_MODE = 0o700
FILE_MODE = 0o600


@dataclass(frozen=True)
class Paths:
    home: Path

    # User files (personal; created by onboarding, never by upgrades)
    @property
    def config(self) -> Path:
        return self.home / "config.yaml"

    @property
    def profile(self) -> Path:
        return self.home / "profile.yaml"

    @property
    def preferences(self) -> Path:
        return self.home / "preferences.yaml"

    @property
    def answers(self) -> Path:
        return self.home / "answers.yaml"

    @property
    def watchlist(self) -> Path:
        return self.home / "watchlist.yaml"

    @property
    def new_questions(self) -> Path:
        return self.home / "new_questions.yaml"

    @property
    def resume_dir(self) -> Path:
        return self.home / "resume"

    @property
    def db(self) -> Path:
        return self.home / "jobhunt.db"

    @property
    def lock(self) -> Path:
        return self.home / ".lock"

    # Work area for the plan -> validate -> execute handoff
    @property
    def work(self) -> Path:
        return self.home / "work"

    @property
    def tasks(self) -> Path:
        return self.work / "tasks"

    @property
    def results(self) -> Path:
        return self.work / "results"

    @property
    def screens(self) -> Path:
        return self.work / "screens"

    @property
    def out(self) -> Path:
        """Where oversized command output is spilled."""
        return self.work / "out"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def browser_profile(self) -> Path:
        return self.home / "browser-profile"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    def directories(self) -> list[Path]:
        return [
            self.home,
            self.resume_dir,
            self.work,
            self.tasks,
            self.results,
            self.screens,
            self.out,
            self.cache,
            self.browser_profile,
            self.logs,
        ]

    def missing_directories(self) -> list[Path]:
        return [d for d in self.directories() if not d.is_dir()]

    def ensure(self) -> list[Path]:
        """Create any missing directories (owner-only). Returns the ones created."""
        created = []
        for d in self.directories():
            if not d.is_dir():
                d.mkdir(parents=True, exist_ok=True)
                created.append(d)
            _chmod(d, DIR_MODE)
        return created


def _chmod(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError):  # e.g. Windows or a filesystem without POSIX modes
        os.chmod(path, mode)


def secure_file(path: Path) -> None:
    """Restrict a personal-data file to its owner."""
    _chmod(path, FILE_MODE)


def get_paths() -> Paths:
    raw = os.environ.get(ENV_HOME)
    home = Path(raw).expanduser() if raw else Path.home() / ".jobhunt"
    return Paths(home=home.resolve())


def package_file(*parts: str) -> Path:
    """A file shipped inside the package (the system layer)."""
    return Path(str(resources.files("jobhunt").joinpath(*parts)))
