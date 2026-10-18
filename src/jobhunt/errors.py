"""Exit codes and the error types that map to them. Agents branch on these, so they are stable."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # generic failure
    USAGE = 2  # bad flags or arguments
    CONFIG = 3  # missing, invalid or placeholder config / onboarding not complete
    VALIDATION = 4  # input or result file failed validation
    NETWORK = 5  # a source or service could not be reached
    NOT_FOUND = 6  # the named job, task or file does not exist
    LOCKED = 7  # another jobhunt process holds the lock
    NEEDS_USER = 8  # stopped on something only the human can do
    CHECKS_FAILED = 9  # doctor found failing checks
    INTERNAL = 70  # a bug in jobhunt


EXIT_CODE_MEANINGS: dict[int, str] = {
    ExitCode.OK: "success",
    ExitCode.ERROR: "generic failure",
    ExitCode.USAGE: "bad flags or arguments",
    ExitCode.CONFIG: "missing, invalid or placeholder config, or onboarding not complete",
    ExitCode.VALIDATION: "input or result file failed validation",
    ExitCode.NETWORK: "a source or service could not be reached",
    ExitCode.NOT_FOUND: "the named job, task or file does not exist",
    ExitCode.LOCKED: "another jobhunt process holds the lock",
    ExitCode.NEEDS_USER: "stopped on something only the human can do",
    ExitCode.CHECKS_FAILED: "doctor found failing checks",
    ExitCode.INTERNAL: "internal error (a bug in jobhunt)",
}


class JobhuntError(Exception):
    code = "error"
    exit_code = ExitCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        next_actions: list[str] | None = None,
        user_actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.next_actions = next_actions or []
        self.user_actions = user_actions or []


class ConfigError(JobhuntError):
    code = "config_error"
    exit_code = ExitCode.CONFIG


class OnboardingIncomplete(ConfigError):
    code = "onboarding_incomplete"


class ValidationFailed(JobhuntError):
    code = "validation_failed"
    exit_code = ExitCode.VALIDATION


class NetworkError(JobhuntError):
    code = "network_error"
    exit_code = ExitCode.NETWORK


class NotFound(JobhuntError):
    code = "not_found"
    exit_code = ExitCode.NOT_FOUND


class LockBusy(JobhuntError):
    code = "locked"
    exit_code = ExitCode.LOCKED


class NeedsUser(JobhuntError):
    code = "needs_user"
    exit_code = ExitCode.NEEDS_USER
