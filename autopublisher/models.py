"""Domain model: publishing jobs, shorts, state transitions and YouTube metadata rules.

Pure Python (no AWS dependencies) so it can be unit-tested locally.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Job lifecycle
STATUS_ANALYZING = "ANALYZING"
STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_PUBLISHING = "PUBLISHING"
STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"

VALID_TRANSITIONS = {
    STATUS_ANALYZING: {STATUS_PENDING_REVIEW, STATUS_ERROR},
    STATUS_PENDING_REVIEW: {STATUS_ANALYZING, STATUS_APPROVED, STATUS_ERROR},
    STATUS_APPROVED: {STATUS_PUBLISHING, STATUS_ERROR},
    STATUS_PUBLISHING: {STATUS_DONE, STATUS_ERROR},
    STATUS_DONE: set(),
    STATUS_ERROR: {STATUS_ANALYZING},
}

# YouTube hard limits
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000
MAX_TAGS_TOTAL_LEN = 470  # actual limit is ~500, keep a margin
MAX_SHORT_SECONDS = 179  # Shorts must stay under 3 minutes


@dataclass
class Short:
    short_id: str
    start: float
    end: float
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    reason: str = ""  # why the model picked this segment (shown in the web UI)
    s3_key: str = ""  # rendered 9:16 clip, set by the cut step
    youtube_id: str = ""
    published_at: str = ""  # ISO timestamp, set by the publisher

    @property
    def duration(self) -> float:
        return self.end - self.start

    def validate(self) -> list[str]:
        problems = []
        if self.end <= self.start:
            problems.append(f"{self.short_id}: end ({self.end}) must be after start ({self.start})")
        if self.duration > MAX_SHORT_SECONDS:
            problems.append(
                f"{self.short_id}: {self.duration:.0f}s exceeds the {MAX_SHORT_SECONDS}s Shorts limit"
            )
        return problems


@dataclass
class MainVideo:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    youtube_id: str = ""  # set at unlisted upload, or provided for pre-existing videos
    unlisted_at: str = ""
    public_at: str = ""


@dataclass
class Job:
    job_id: str
    video_key: str  # S3 key of the original video under incoming/
    status: str = STATUS_ANALYZING
    created_at: str = ""
    existing_youtube_id: str = ""  # non-empty → video is already on YouTube, publish shorts only
    language: str = ""  # ISO code detected by Transcribe or forced by the user
    language_override: str = ""
    has_speech: bool = False
    prompt: str = ""  # free-text guidance for the analysis step
    main: MainVideo = field(default_factory=MainVideo)
    shorts: list[Short] = field(default_factory=list)
    error: str = ""

    def transition(self, new_status: str) -> None:
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(f"invalid transition {self.status} -> {new_status}")
        self.status = new_status

    def pending_shorts(self) -> list[Short]:
        return [s for s in self.shorts if not s.youtube_id]

    def all_shorts_published(self) -> bool:
        return bool(self.shorts) and not self.pending_shorts()

    def main_video_link(self) -> str:
        video_id = self.existing_youtube_id or self.main.youtube_id
        return f"https://youtu.be/{video_id}" if video_id else ""

    def to_item(self) -> dict:
        return asdict(self)

    @classmethod
    def from_item(cls, item: dict) -> Job:
        data = dict(item)
        data["main"] = MainVideo(**data.get("main") or {})
        data["shorts"] = [Short(**s) for s in data.get("shorts") or []]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def sanitize_title(title: str) -> str:
    """YouTube titles: max 100 chars, no angle brackets."""
    clean = re.sub(r"[<>]", "", title).strip()
    return clean[:MAX_TITLE_LEN].strip()


def sanitize_description(description: str) -> str:
    clean = re.sub(r"[<>]", "", description).strip()
    return clean[:MAX_DESCRIPTION_LEN]


def sanitize_tags(tags: list[str]) -> list[str]:
    """Deduplicate and trim so the total stays within YouTube's tag budget."""
    result: list[str] = []
    total = 0
    seen = set()
    for tag in tags:
        clean = re.sub(r"[<>]", "", tag).strip()
        if not clean or clean.lower() in seen:
            continue
        # tags containing spaces count with surrounding quotes
        cost = len(clean) + (2 if " " in clean else 0) + 1
        if total + cost > MAX_TAGS_TOTAL_LEN:
            break
        seen.add(clean.lower())
        result.append(clean)
        total += cost
    return result


def short_description_with_link(short: Short, main_link: str) -> str:
    """Final description for a published Short: its own text plus the full-video link."""
    parts = [short.description.strip()]
    if main_link:
        parts.append(f"Full video: {main_link}")
    return sanitize_description("\n\n".join(p for p in parts if p))


def next_short_to_publish(jobs: list[Job]) -> tuple[Job, Short] | None:
    """Pick the next Short across all publishing jobs: oldest job first, in segment order.

    One Short per scheduler run keeps the channel cadence (default: daily) and stays
    far below the YouTube API upload quota.
    """
    candidates = [j for j in jobs if j.status == STATUS_PUBLISHING and j.pending_shorts()]
    if not candidates:
        return None
    job = min(candidates, key=lambda j: j.created_at)
    return job, job.pending_shorts()[0]
