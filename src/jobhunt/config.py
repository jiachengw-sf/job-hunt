"""The one config loader. System defaults (package) are deep-merged with the user layer
(``~/.jobhunt/config.yaml``), then validated. User files (profile, preferences, answers) load
here too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from jobhunt import __version__
from jobhunt.errors import ConfigError, OnboardingIncomplete
from jobhunt.models import Answers, Config, Preferences, Profile, Watchlist
from jobhunt.paths import Paths, get_paths, package_file

M = TypeVar("M", bound=BaseModel)

PLACEHOLDER_RE = re.compile(r"CHANGE_?ME|^<[^<>]+>$|^TODO$", re.IGNORECASE)

USER_FILES: dict[str, type[BaseModel]] = {
    "profile": Profile,
    "preferences": Preferences,
    "answers": Answers,
}


def read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(f"{path} does not exist.") from e
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(
            f"{path} is not valid YAML{where}: {getattr(e, 'problem', e)}",
            next_actions=[f"Fix the YAML syntax in {path}."],
        ) from e


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_placeholders(data: Any, prefix: str = "") -> list[str]:
    """Dotted paths of every string value that is still a template placeholder."""
    found: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            found += find_placeholders(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            found += find_placeholders(v, f"{prefix}[{i}]")
    elif isinstance(data, str) and PLACEHOLDER_RE.search(data.strip()):
        found.append(prefix)
    return found


def format_validation_error(path: Path, err: PydanticValidationError) -> str:
    parts = []
    for e in err.errors()[:10]:
        loc = ".".join(str(p) for p in e["loc"]) or "(top level)"
        parts.append(f"{loc}: {e['msg']}")
    more = f" (+{err.error_count() - 10} more)" if err.error_count() > 10 else ""
    return f"{path} is invalid: " + "; ".join(parts) + more


def validate(model: type[M], data: Any, path: Path) -> M:
    try:
        return model.model_validate(data if data is not None else {})
    except PydanticValidationError as e:
        raise ConfigError(
            format_validation_error(path, e), next_actions=[f"Fix the listed keys in {path}."]
        ) from e


def default_config_data() -> dict[str, Any]:
    return read_yaml(package_file("defaults", "config.yaml")) or {}


def load_config(paths: Paths | None = None) -> Config:
    paths = paths or get_paths()
    data = default_config_data()
    if paths.config.exists():
        user = read_yaml(paths.config) or {}
        if not isinstance(user, dict):
            raise ConfigError(f"{paths.config} must be a mapping of settings.")
        data = deep_merge(data, user)
        if ph := find_placeholders(user):
            raise ConfigError(
                f"{paths.config} still has placeholder values at: {', '.join(ph)}",
                next_actions=[f"Replace or remove those keys in {paths.config}."],
            )
    data["http"]["user_agent"] = data["http"]["user_agent"].replace("{version}", __version__)
    return validate(Config, data, paths.config)


def tracker_xlsx_path(cfg: Config, paths: Paths) -> Path:
    raw = cfg.tracker.xlsx_path
    return Path(raw).expanduser() if raw else paths.home / "tracker.xlsx"


def load_user_file(kind: str, paths: Paths | None = None) -> BaseModel:
    paths = paths or get_paths()
    model = USER_FILES[kind]
    path: Path = getattr(paths, kind)
    if not path.exists():
        raise OnboardingIncomplete(
            f"{path} is missing; onboarding has not been completed.",
            next_actions=["Run the jobhunt-setup skill to finish onboarding."],
        )
    data = read_yaml(path)
    if ph := find_placeholders(data):
        raise ConfigError(
            f"{path} still has placeholder values at: {', '.join(ph[:10])}",
            next_actions=[f"Fill in or remove those keys in {path}."],
        )
    return validate(model, data, path)


def load_watchlist(paths: Paths | None = None) -> Watchlist:
    """watchlist.yaml is optional: a missing file is an empty watchlist."""
    paths = paths or get_paths()
    if not paths.watchlist.exists():
        return Watchlist()
    data = read_yaml(paths.watchlist)
    if ph := find_placeholders(data):
        raise ConfigError(
            f"{paths.watchlist} still has placeholder values at: {', '.join(ph[:10])}",
            next_actions=[f"Fill in or remove those keys in {paths.watchlist}."],
        )
    return validate(Watchlist, data, paths.watchlist)


def onboarding_status(paths: Paths | None = None) -> dict[str, str]:
    """``{file: ok | missing | invalid: <reason>}`` for each onboarding file."""
    paths = paths or get_paths()
    status: dict[str, str] = {}
    for kind in USER_FILES:
        try:
            load_user_file(kind, paths)
            status[kind] = "ok"
        except OnboardingIncomplete:
            status[kind] = "missing"
        except ConfigError as e:
            status[kind] = f"invalid: {e.message}"
    return status


def require_onboarding(paths: Paths | None = None) -> None:
    """Gate for every command except doctor/schema/setup: nothing runs before onboarding."""
    status = onboarding_status(paths)
    bad = {k: v for k, v in status.items() if v != "ok"}
    if bad:
        raise OnboardingIncomplete(
            "Onboarding is not complete: " + "; ".join(f"{k} {v}" for k, v in bad.items()),
            details={"onboarding": status},
            next_actions=["Run the jobhunt-setup skill to finish onboarding."],
        )
