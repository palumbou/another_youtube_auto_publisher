"""Domain model: jobs, immutable revisions, assets, reviews, schedules,
publications and the append-only audit trail. Pure Python, no AWS."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

# --- Job states ------------------------------------------------------------------

INGESTED = "INGESTED"
VALIDATING = "VALIDATING"
ANALYZING = "ANALYZING"
GENERATING_ASSETS = "GENERATING_ASSETS"
AWAITING_REVIEW = "AWAITING_REVIEW"
REPROCESS_REQUESTED = "REPROCESS_REQUESTED"
APPROVED = "APPROVED"
SCHEDULED = "SCHEDULED"
UPLOADING_PRIVATE = "UPLOADING_PRIVATE"
VERIFYING = "VERIFYING"
UPLOADED_PRIVATE = "UPLOADED_PRIVATE"  # honest ceiling for an unaudited API project
PUBLISHED = "PUBLISHED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

STATES = (INGESTED, VALIDATING, ANALYZING, GENERATING_ASSETS, AWAITING_REVIEW,
          REPROCESS_REQUESTED, APPROVED, SCHEDULED, UPLOADING_PRIVATE, VERIFYING,
          UPLOADED_PRIVATE, PUBLISHED, FAILED, CANCELLED)

TRANSITIONS: dict[str, frozenset[str]] = {
    INGESTED: frozenset({VALIDATING}),
    VALIDATING: frozenset({ANALYZING, FAILED}),
    ANALYZING: frozenset({GENERATING_ASSETS, FAILED}),
    GENERATING_ASSETS: frozenset({AWAITING_REVIEW, FAILED}),
    AWAITING_REVIEW: frozenset({REPROCESS_REQUESTED, APPROVED, CANCELLED}),
    REPROCESS_REQUESTED: frozenset({ANALYZING, GENERATING_ASSETS}),
    APPROVED: frozenset({SCHEDULED, CANCELLED}),
    SCHEDULED: frozenset({UPLOADING_PRIVATE, CANCELLED}),
    UPLOADING_PRIVATE: frozenset({VERIFYING, FAILED}),
    VERIFYING: frozenset({PUBLISHED, UPLOADED_PRIVATE, FAILED}),
    UPLOADED_PRIVATE: frozenset(),
    PUBLISHED: frozenset(),
    # A failed job can only be revived by an authenticated redrive into a new revision.
    FAILED: frozenset({REPROCESS_REQUESTED}),
    CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset({PUBLISHED, UPLOADED_PRIVATE, CANCELLED})
UNPUBLISHED_STATES = frozenset(STATES) - frozenset({UPLOADING_PRIVATE, VERIFYING, UPLOADED_PRIVATE, PUBLISHED})

# --- Reprocess scopes -------------------------------------------------------------

METADATA_ONLY = "METADATA_ONLY"
SHORTS_ONLY = "SHORTS_ONLY"
TRANSCRIPT_AND_ANALYSIS = "TRANSCRIPT_AND_ANALYSIS"
FULL = "FULL"
SCOPES = (METADATA_ONLY, SHORTS_ONLY, TRANSCRIPT_AND_ANALYSIS, FULL)

SCOPE_ENTRY_STATE = {
    METADATA_ONLY: ANALYZING,
    TRANSCRIPT_AND_ANALYSIS: ANALYZING,
    FULL: ANALYZING,
    SHORTS_ONLY: GENERATING_ASSETS,
}

# --- Review statuses ------------------------------------------------------------------

PENDING = "PENDING"
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED = "REJECTED"

# --- Asset types --------------------------------------------------------------------------

MASTER = "MASTER"
SHORT = "SHORT"
THUMBNAIL = "THUMBNAIL"


class InvalidTransition(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _from_dict(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Asset:
    asset_id: str
    type: str
    key: str = ""
    version_id: str = ""
    sha256: str = ""
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    codec: str = ""
    size_bytes: int = 0
    source_start_ms: int = 0
    source_end_ms: int = 0
    segment_ids: list[str] = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    review_status: str = PENDING
    review_reason: str = ""
    metadata: dict = field(default_factory=dict)  # proposed and, after edits, reviewed metadata

    @classmethod
    def from_dict(cls, data: dict) -> Asset:
        return _from_dict(cls, data)


@dataclass
class Revision:
    job_id: str
    project_key: str
    number: int
    scope: str = FULL
    parent_number: int = 0
    created_at: str = field(default_factory=now_iso)
    transcript_key: str = ""
    transcript_language: str = ""
    transcript_provider: str = ""
    transcript_confidence: float = 0.0
    analysis_key: str = ""
    analysis_provider: str = ""
    prompt_version: str = ""
    analysis: dict = field(default_factory=dict)  # schema-validated structured analysis
    master: Asset | None = None
    shorts: list[Asset] = field(default_factory=list)
    thumbnails: list[Asset] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    checksums: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    command_log_key: str = ""
    master_review_status: str = PENDING
    master_review_reason: str = ""
    master_metadata: dict = field(default_factory=dict)
    reject_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Revision:
        data = dict(data)
        if data.get("master"):
            data["master"] = Asset.from_dict(data["master"])
        data["shorts"] = [Asset.from_dict(a) for a in data.get("shorts") or []]
        data["thumbnails"] = [Asset.from_dict(a) for a in data.get("thumbnails") or []]
        return _from_dict(cls, data)

    def asset(self, asset_id: str) -> Asset | None:
        if self.master and self.master.asset_id == asset_id:
            return self.master
        for a in self.shorts + self.thumbnails:
            if a.asset_id == asset_id:
                return a
        return None

    def approved_assets(self) -> list[Asset]:
        out = []
        if self.master and self.master_review_status == REVIEW_APPROVED:
            out.append(self.master)
        out.extend(a for a in self.shorts if a.review_status == REVIEW_APPROVED)
        return out


@dataclass
class Job:
    project_key: str
    job_id: str
    environment: str
    idempotency_key: str
    source_key: str
    source_sha256: str
    source_version_id: str
    ready_version_id: str
    ready_created_at: str
    manifest_key: str = ""
    manifest_version_id: str = ""
    manifest_sha256: str = ""
    channel_profile: str = ""
    language: str = ""
    cutover_decision: str = ""
    state: str = INGESTED
    current_revision: int = 0
    version: int = 1  # optimistic lock, incremented on every persisted change
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    source_duration_ms: int = 0
    source_width: int = 0
    source_height: int = 0
    reprocess_scope: str = ""
    error: dict = field(default_factory=dict)
    summary: str = ""

    @property
    def pk(self) -> str:
        return f"{self.project_key}#{self.job_id}"

    def can_transition(self, new_state: str) -> bool:
        return new_state in TRANSITIONS.get(self.state, frozenset())

    def transition(self, new_state: str) -> str:
        if not self.can_transition(new_state):
            raise InvalidTransition(f"{self.pk}: {self.state} -> {new_state} is not allowed")
        old, self.state = self.state, new_state
        self.updated_at = now_iso()
        return old

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        return _from_dict(cls, data)


@dataclass
class Schedule:
    schedule_id: str
    project_key: str
    job_id: str
    revision: int
    asset_id: str
    publish_at_utc: str
    owner_timezone: str
    privacy_after_publish: str = "private"
    notify_subscribers: bool = False
    status: str = "PENDING"  # PENDING | QUEUED | UPLOADED | VERIFIED | FAILED | CANCELLED
    attempts: int = 0
    last_error: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Schedule:
        return _from_dict(cls, data)


@dataclass
class Publication:
    publication_id: str  # deterministic: project#job#revision#asset
    project_key: str
    job_id: str
    revision: int
    asset_id: str
    youtube_video_id: str = ""
    privacy_status: str = ""
    upload_session_url: str = ""  # never logged; kept for resumption only
    uploaded_bytes: int = 0
    total_bytes: int = 0
    processing_status: str = ""
    verified_metadata: dict = field(default_factory=dict)
    publish_at_utc: str = ""
    mode: str = "fake"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Publication:
        return _from_dict(cls, data)


@dataclass
class AuditEvent:
    project_key: str
    job_id: str
    action: str
    actor: str
    old_state: str = ""
    new_state: str = ""
    revision: int = 0
    reason: str = ""
    source_ip: str = ""
    details: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)
    sequence: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AuditEvent:
        return _from_dict(cls, data)


def idempotency_key(environment: str, project_key: str, job_id: str,
                    source_sha256: str, source_version_id: str) -> str:
    raw = f"{environment}\n{project_key}\n{job_id}\n{source_sha256}\n{source_version_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def scrub(value: Any) -> Any:
    """Remove fields that must never reach a receipt or a log."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if k not in ("upload_session_url", "refresh_token", "access_token", "client_secret")}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value
