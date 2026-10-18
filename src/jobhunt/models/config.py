from __future__ import annotations

from typing import Literal

from pydantic import Field

from jobhunt.models.base import Strict


class SheetsConfig(Strict):
    spreadsheet_id: str | None = None
    service_account_file: str | None = None


class TrackerConfig(Strict):
    kind: Literal["xlsx", "sheets", "none"] = "xlsx"
    xlsx_path: str | None = Field(None, description="Default: tracker.xlsx in the data directory.")
    sheets: SheetsConfig = Field(default_factory=SheetsConfig)


class HttpConfig(Strict):
    user_agent: str
    min_host_interval_seconds: float = Field(1.0, ge=1.0)
    timeout_seconds: float = Field(20, gt=0)
    max_retries: int = Field(4, ge=0, le=10)


class SimplifyConfig(Strict):
    enabled: bool = True
    branch: str = "dev"
    internship_repo: str = Field(
        "Summer2027-Internships", description="Used when preferences seek internships or co-ops."
    )
    new_grad_repo: str = Field(
        "New-Grad-Positions", description="Used when preferences seek new-grad or entry roles."
    )


class DiscoveryConfig(Strict):
    simplify: SimplifyConfig = Field(default_factory=SimplifyConfig)
    old_posting_days: int = Field(60, ge=1, description="Flag postings older than this.")
    repost_max_distance: int = Field(
        3, ge=0, le=16, description="SimHash bits that may differ for a repost match."
    )
    empty_run_warn_after: int = Field(
        3, ge=1, description="Warn when a source returns nothing this many runs in a row."
    )


class ScheduleConfig(Strict):
    interval_hours: float = Field(4, gt=0)


class FitConfig(Strict):
    recall_top_k: int = Field(50, ge=1)
    priority_threshold: float = Field(0.5, ge=0, le=1)


class VolumeGuardConfig(Strict):
    max_applications_per_company: int = Field(2, ge=1)
    window_days: int = Field(90, ge=1)


class PrefillConfig(Strict):
    batch_size: int = Field(5, ge=1, le=20)


class FollowupsConfig(Strict):
    after_days: int = Field(7, ge=1)
    max_per_application: int = Field(2, ge=0)


class ResearchConfig(Strict):
    cache_days: int = Field(14, ge=0)
    max_searches: int = Field(5, ge=0, le=5)
    max_fetches: int = Field(8, ge=0, le=8)


class SmtpConfig(Strict):
    host: str
    port: int = 587
    username: str | None = None
    password_env: str | None = Field(
        None,
        description="Name of an environment variable holding the password. Never the password.",
    )
    to: str
    starttls: bool = True


class NotificationsConfig(Strict):
    ntfy_topic: str | None = None
    discord_webhook: str | None = None
    smtp: SmtpConfig | None = None


class Config(Strict):
    version: Literal[1] = 1
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    http: HttpConfig
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    fit: FitConfig = Field(default_factory=FitConfig)
    volume_guard: VolumeGuardConfig = Field(default_factory=VolumeGuardConfig)
    prefill: PrefillConfig = Field(default_factory=PrefillConfig)
    followups: FollowupsConfig = Field(default_factory=FollowupsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    writing_style_skill: str | None = None
