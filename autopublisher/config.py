"""Runtime settings, read once from the environment. No secret is stored here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

LOCAL_ENVIRONMENTS = ("local",)
DEFAULT_CUTOVER = datetime(2099, 1, 1, tzinfo=UTC)  # nothing is eligible until an owner sets it


class ConfigurationError(RuntimeError):
    pass


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ConfigurationError(f"timestamp {value!r} must carry a timezone")
    return parsed.astimezone(UTC)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    environment: str = "local"
    local_mode: bool = False
    cutover_at: datetime = DEFAULT_CUTOVER
    allowed_projects: tuple[str, ...] = ("quiz-al-volo",)
    incoming_prefix: str = "incoming/"
    max_source_bytes: int = 8 * 1024**3
    max_source_duration_ms: int = 4 * 3600 * 1000
    owner_timezone: str = "Europe/Rome"
    publish_kill_switch: bool = True
    youtube_mode: str = "fake"  # "fake" | "real"
    api_project_audited: bool = False
    daily_upload_cap: int = 3
    weekly_upload_cap: int = 10
    max_shorts_per_job: int = 4
    ip_allowlist: tuple[str, ...] = ()
    video_encoder: str = "libx264"
    manifest_required_projects: tuple[str, ...] = ("quiz-al-volo",)
    allow_unstructured_analysis: bool = False

    def __post_init__(self) -> None:
        if self.local_mode and self.environment not in LOCAL_ENVIRONMENTS:
            raise ConfigurationError(
                f"LOCAL_MODE is enabled but ENVIRONMENT is {self.environment!r}; refusing to start"
            )
        if self.youtube_mode not in ("fake", "real"):
            raise ConfigurationError(f"YOUTUBE_MODE must be fake or real, got {self.youtube_mode!r}")
        if self.youtube_mode == "real" and self.local_mode:
            raise ConfigurationError("real YouTube mode is not allowed together with LOCAL_MODE")
        if not self.incoming_prefix.endswith("/"):
            raise ConfigurationError("INCOMING_PREFIX must end with '/'")

    @classmethod
    def from_env(cls, env: dict | None = None) -> Settings:
        e = os.environ if env is None else env
        try:
            cutover = parse_utc(e["CUTOVER_AT"]) if e.get("CUTOVER_AT") else DEFAULT_CUTOVER
        except ValueError as exc:
            raise ConfigurationError(f"CUTOVER_AT is not an RFC 3339 timestamp: {exc}") from exc
        return cls(
            environment=e.get("ENVIRONMENT", "local"),
            local_mode=_bool(e.get("LOCAL_MODE")),
            cutover_at=cutover,
            allowed_projects=_csv(e.get("ALLOWED_PROJECTS")) or cls.allowed_projects,
            incoming_prefix=e.get("INCOMING_PREFIX", cls.incoming_prefix),
            max_source_bytes=int(e.get("MAX_SOURCE_BYTES", cls.max_source_bytes)),
            max_source_duration_ms=int(e.get("MAX_SOURCE_DURATION_MS", cls.max_source_duration_ms)),
            owner_timezone=e.get("OWNER_TIMEZONE", cls.owner_timezone),
            publish_kill_switch=_bool(e.get("PUBLISH_KILL_SWITCH"), default=True),
            youtube_mode=e.get("YOUTUBE_MODE", cls.youtube_mode),
            api_project_audited=_bool(e.get("YOUTUBE_API_PROJECT_AUDITED")),
            daily_upload_cap=int(e.get("DAILY_UPLOAD_CAP", cls.daily_upload_cap)),
            weekly_upload_cap=int(e.get("WEEKLY_UPLOAD_CAP", cls.weekly_upload_cap)),
            max_shorts_per_job=int(e.get("MAX_SHORTS_PER_JOB", cls.max_shorts_per_job)),
            ip_allowlist=_csv(e.get("REVIEW_IP_ALLOWLIST")),
            video_encoder=e.get("VIDEO_ENCODER", cls.video_encoder),
            manifest_required_projects=_csv(e.get("MANIFEST_REQUIRED_PROJECTS")) or cls.manifest_required_projects,
            allow_unstructured_analysis=_bool(e.get("ALLOW_UNSTRUCTURED_ANALYSIS")),
        )
