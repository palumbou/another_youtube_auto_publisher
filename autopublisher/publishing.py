"""Approval-gated publishing: schedules become private uploads, video ids are
persisted exactly once, and nothing goes public automatically."""

from __future__ import annotations

import json
import secrets
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autopublisher.config import Settings, parse_utc
from autopublisher.domain import (
    PUBLISHED,
    REVIEW_APPROVED,
    SCHEDULED,
    UPLOADED_PRIVATE,
    UPLOADING_PRIVATE,
    VERIFYING,
    AuditEvent,
    Job,
    Publication,
    Revision,
    Schedule,
    now_iso,
)
from autopublisher.jobstore import AlreadyExists, JobStore
from autopublisher.objectstore import ObjectStore
from autopublisher.youtube import YouTubeAuthError, YouTubeClient, YouTubeError, YouTubeQuotaError

QUOTA_UNITS_PER_UPLOAD = 1600  # videos.insert cost documented by the Data API
DEFAULT_DAILY_QUOTA_UNITS = 10_000


# --- Adapters -----------------------------------------------------------------------------

@dataclass
class UploadResult:
    video_id: str
    privacy_status: str
    processing_status: str = "processing"


class YouTubeAdapter(ABC):
    mode = "abstract"

    @abstractmethod
    def upload(self, source: Path, metadata: dict, privacy: str, publish_at: str, notify: bool) -> UploadResult: ...

    @abstractmethod
    def get_status(self, video_id: str) -> dict: ...

    @abstractmethod
    def set_thumbnail(self, video_id: str, image: bytes) -> None: ...


class FakeYouTubeAdapter(YouTubeAdapter):
    """Records every request. Optional scripted failures for the tests."""

    mode = "fake"

    def __init__(self, fail_with: Exception | None = None, fail_times: int = 0):
        self.requests: list[dict] = []
        self.videos: dict[str, dict] = {}
        self.fail_with, self.fail_times = fail_with, fail_times

    def upload(self, source: Path, metadata: dict, privacy: str, publish_at: str, notify: bool) -> UploadResult:
        self.requests.append({"op": "upload", "source": str(source), "metadata": metadata, "privacy": privacy,
                              "publish_at": publish_at, "notify": notify})
        if self.fail_with and self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with
        video_id = "fake-" + secrets.token_hex(4)
        self.videos[video_id] = {"privacyStatus": privacy, "publishAt": publish_at, "metadata": metadata,
                                 "processing": "succeeded"}
        return UploadResult(video_id, privacy, "succeeded")

    def get_status(self, video_id: str) -> dict:
        self.requests.append({"op": "status", "video_id": video_id})
        video = self.videos.get(video_id)
        if video is None:
            raise YouTubeError(f"video {video_id} not found")
        return {"status": {"privacyStatus": video["privacyStatus"], "publishAt": video["publishAt"]},
                "snippet": {"title": video["metadata"]["title"]},
                "processingDetails": {"processingStatus": video["processing"]}}

    def set_thumbnail(self, video_id: str, image: bytes) -> None:
        self.requests.append({"op": "thumbnail", "video_id": video_id, "bytes": len(image)})


class RealYouTubeAdapter(YouTubeAdapter):
    """OAuth-authorized Data API client. Secrets come from Secrets Manager in AWS
    and are never logged."""

    mode = "real"

    def __init__(self, client: YouTubeClient):
        self.client = client

    @classmethod
    def from_secret(cls, secret_json: str) -> RealYouTubeAdapter:
        creds = json.loads(secret_json)
        return cls(YouTubeClient(creds["client_id"], creds["client_secret"], creds["refresh_token"]))

    def upload(self, source: Path, metadata: dict, privacy: str, publish_at: str, notify: bool) -> UploadResult:
        size = Path(source).stat().st_size
        with open(source, "rb") as handle:
            video_id = self.client.upload(handle, size, metadata=metadata, privacy=privacy,
                                          publish_at=publish_at, notify_subscribers=notify)
        return UploadResult(video_id, privacy)

    def get_status(self, video_id: str) -> dict:
        return self.client.get_video(video_id)

    def set_thumbnail(self, video_id: str, image: bytes) -> None:
        self.client.set_thumbnail(video_id, image)


# --- Quota ----------------------------------------------------------------------------------------

@dataclass
class QuotaReport:
    uploads_today: int
    uploads_this_week: int
    units_today: int
    daily_cap: int
    weekly_cap: int
    daily_units: int = DEFAULT_DAILY_QUOTA_UNITS

    @property
    def allowed(self) -> bool:
        return (self.uploads_today < self.daily_cap and self.uploads_this_week < self.weekly_cap
                and self.units_today + QUOTA_UNITS_PER_UPLOAD <= self.daily_units)

    def as_dict(self) -> dict:
        return {**self.__dict__, "allowed": self.allowed}


def quota_report(jobs: JobStore, settings: Settings, now: datetime) -> QuotaReport:
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)
    today = week = 0
    for pub in jobs.list_publications():
        if not pub.youtube_video_id or pub.mode != "real":
            continue
        created = parse_utc(pub.created_at)
        if created >= day_start:
            today += 1
        if created >= week_start:
            week += 1
    return QuotaReport(today, week, today * QUOTA_UNITS_PER_UPLOAD, settings.daily_upload_cap, settings.weekly_upload_cap)


# --- Publisher ----------------------------------------------------------------------------------------

class PublishBlocked(RuntimeError):
    pass


@dataclass
class PublishOutcome:
    schedule_id: str
    status: str
    video_id: str = ""
    message: str = ""
    details: dict = field(default_factory=dict)


class Publisher:
    def __init__(self, settings: Settings, store: ObjectStore, jobs: JobStore, adapter: YouTubeAdapter,
                 workdir: Path | None = None):
        self.settings, self.store, self.jobs, self.adapter = settings, store, jobs, adapter
        self.workdir = workdir
        if adapter.mode == "real" and settings.youtube_mode != "real":
            raise PublishBlocked("a real adapter requires YOUTUBE_MODE=real")

    def _audit(self, job: Job, action: str, old: str, actor: str = "system:publisher", **details) -> None:
        self.jobs.append_audit(AuditEvent(project_key=job.project_key, job_id=job.job_id, action=action, actor=actor,
                                          old_state=old, new_state=job.state, revision=job.current_revision,
                                          details=details))

    # -- guards --------------------------------------------------------------------------------------

    def _guards(self, now: datetime) -> QuotaReport:
        if self.settings.publish_kill_switch:
            raise PublishBlocked("PUBLISH_KILL_SWITCH is on; publishing is paused, review stays available")
        quota = quota_report(self.jobs, self.settings, now)
        if not quota.allowed:
            raise PublishBlocked(f"upload cap or quota reached: {json.dumps(quota.as_dict())}")
        return quota

    @staticmethod
    def _approved_asset(revision: Revision, asset_id: str):
        asset = revision.asset(asset_id)
        status = revision.master_review_status if asset_id == "master" else (asset.review_status if asset else "")
        if asset is None or status != REVIEW_APPROVED:
            raise PublishBlocked(f"{asset_id} is not approved in revision {revision.number}")
        return asset

    def _metadata_for(self, revision: Revision, asset_id: str) -> dict:
        meta = dict(revision.master_metadata if asset_id == "master" else revision.asset(asset_id).metadata)
        if not meta.get("owner_confirmed_audience"):
            raise PublishBlocked("audience decision not confirmed by the owner")
        if meta.get("contains_synthetic_media") and not meta.get("owner_confirmed_synthetic"):
            raise PublishBlocked("synthetic-media declaration not confirmed by the owner")
        return meta

    # -- run ---------------------------------------------------------------------------------------------

    def run_due(self, now: datetime | None = None, actor: str = "system:publisher") -> list[PublishOutcome]:
        now = now or datetime.now(UTC)
        outcomes = []
        try:
            self._guards(now)
        except PublishBlocked as exc:
            return [PublishOutcome("", "BLOCKED", message=str(exc))]
        for schedule in sorted(self.jobs.list_schedules("PENDING"), key=lambda s: s.publish_at_utc):
            if parse_utc(schedule.publish_at_utc) - timedelta(hours=1) > now:
                continue  # upload privately up to an hour early so publishAt can be honoured
            outcomes.append(self.publish_schedule(schedule, now, actor))
            try:
                self._guards(now)
            except PublishBlocked as exc:
                outcomes.append(PublishOutcome("", "BLOCKED", message=str(exc)))
                break
        return outcomes

    def publish_schedule(self, schedule: Schedule, now: datetime, actor: str = "system:publisher") -> PublishOutcome:
        job = self.jobs.get_job(schedule.project_key, schedule.job_id)
        if job is None:
            return PublishOutcome(schedule.schedule_id, "SKIPPED", message="job missing")
        revision = self.jobs.get_revision(job.project_key, job.job_id, schedule.revision)
        if revision is None or revision.number != job.current_revision:
            return self._fail_schedule(schedule, job, "revision is no longer current")
        if job.state not in (SCHEDULED, UPLOADING_PRIVATE, VERIFYING):
            return self._fail_schedule(schedule, job, f"job is {job.state}")
        try:
            asset = self._approved_asset(revision, schedule.asset_id)
            metadata = self._metadata_for(revision, schedule.asset_id)
        except PublishBlocked as exc:
            return self._fail_schedule(schedule, job, str(exc))

        publication_id = f"{job.project_key}#{job.job_id}#{revision.number}#{schedule.asset_id}"
        publication = self.jobs.get_publication(publication_id)
        if publication is None:
            publication = Publication(publication_id=publication_id, project_key=job.project_key, job_id=job.job_id,
                                      revision=revision.number, asset_id=schedule.asset_id, mode=self.adapter.mode,
                                      publish_at_utc=schedule.publish_at_utc, total_bytes=asset.size_bytes)
            try:
                self.jobs.create_publication(publication)
            except AlreadyExists:
                publication = self.jobs.get_publication(publication_id)

        schedule.status, schedule.attempts = "QUEUED", schedule.attempts + 1
        self.jobs.update_schedule(schedule, expected_version=schedule.version)
        if job.state == SCHEDULED:
            old = job.transition(UPLOADING_PRIVATE)
            self.jobs.update_job(job, expected_version=job.version)
            self._audit(job, "UPLOAD_STARTED", old, actor, asset_id=schedule.asset_id, mode=self.adapter.mode)

        # Idempotency: an existing video id means the upload already happened.
        if not publication.youtube_video_id:
            try:
                with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
                    local = self.store.download(asset.key, Path(tmp) / Path(asset.key).name, asset.version_id or None)
                    publish_at = schedule.publish_at_utc if self.settings.api_project_audited else ""
                    result = self.adapter.upload(local, metadata, "private", publish_at, schedule.notify_subscribers)
            except YouTubeAuthError as exc:
                return self._recoverable(schedule, job, "AUTH_REQUIRED", str(exc), actor)
            except YouTubeQuotaError as exc:
                return self._recoverable(schedule, job, "QUOTA_EXHAUSTED", str(exc), actor)
            except YouTubeError as exc:
                return self._recoverable(schedule, job, "UPLOAD_ERROR", str(exc), actor)
            publication.youtube_video_id = result.video_id
            publication.privacy_status = result.privacy_status
            publication.uploaded_bytes = asset.size_bytes
            publication.processing_status = result.processing_status
            publication.updated_at = now_iso()
            self.jobs.update_publication(publication, expected_version=publication.version)
            self._audit(job, "UPLOADED_PRIVATE", job.state, actor, asset_id=schedule.asset_id,
                        youtube_video_id=result.video_id, mode=self.adapter.mode)
            thumb = next((t for t in revision.thumbnails), None)
            if schedule.asset_id == "master" and thumb:
                try:
                    self.adapter.set_thumbnail(result.video_id, self.store.get_bytes(thumb.key))
                except YouTubeError as exc:
                    self._audit(job, "THUMBNAIL_FAILED", job.state, actor, message=str(exc)[:200])

        return self._verify(schedule, job, publication, actor)

    def _verify(self, schedule: Schedule, job: Job, publication: Publication, actor: str) -> PublishOutcome:
        if job.state == UPLOADING_PRIVATE:
            old = job.transition(VERIFYING)
            self.jobs.update_job(job, expected_version=job.version)
            self._audit(job, "VERIFYING", old, actor)
        try:
            remote = self.adapter.get_status(publication.youtube_video_id)
        except YouTubeError as exc:
            return self._recoverable(schedule, job, "VERIFY_ERROR", str(exc), actor)
        publication.verified_metadata = {"privacyStatus": remote.get("status", {}).get("privacyStatus"),
                                         "publishAt": remote.get("status", {}).get("publishAt", ""),
                                         "title": remote.get("snippet", {}).get("title", ""),
                                         "processingStatus": remote.get("processingDetails", {}).get("processingStatus", "")}
        publication.processing_status = publication.verified_metadata["processingStatus"]
        self.jobs.update_publication(publication, expected_version=publication.version)
        schedule.status = "UPLOADED"
        self.jobs.update_schedule(schedule, expected_version=schedule.version)
        # Final state: PUBLISHED only when the scheduled release could actually be requested
        # (audited API project); otherwise the honest ceiling is UPLOADED_PRIVATE.
        pending = [s for s in self.jobs.list_schedules() if s.job_id == job.job_id and s.project_key == job.project_key
                   and s.status in ("PENDING", "QUEUED")]
        if not pending:
            final = PUBLISHED if self.settings.api_project_audited and publication.verified_metadata["publishAt"] else UPLOADED_PRIVATE
            old = job.transition(final)
            self.jobs.update_job(job, expected_version=job.version)
            self._audit(job, "FINAL", old, actor, youtube_video_id=publication.youtube_video_id,
                        api_project_audited=self.settings.api_project_audited)
        return PublishOutcome(schedule.schedule_id, "UPLOADED", publication.youtube_video_id,
                              details=publication.verified_metadata)

    def _recoverable(self, schedule: Schedule, job: Job, status: str, message: str, actor: str) -> PublishOutcome:
        schedule.status, schedule.last_error = status, message[:500]
        self.jobs.update_schedule(schedule, expected_version=schedule.version)
        self._audit(job, "PUBLISH_" + status, job.state, actor, message=message[:300])
        return PublishOutcome(schedule.schedule_id, status, message=message)

    def _fail_schedule(self, schedule: Schedule, job: Job, message: str) -> PublishOutcome:
        schedule.status, schedule.last_error = "FAILED", message[:500]
        self.jobs.update_schedule(schedule, expected_version=schedule.version)
        self._audit(job, "PUBLISH_REFUSED", job.state, message=message[:300])
        return PublishOutcome(schedule.schedule_id, "FAILED", message=message)


def find_duplicate_uploads(jobs: JobStore) -> list[dict]:
    """Reconciliation helper: assets with more than one video id need a human decision."""
    seen: dict[tuple[str, str, str], list[str]] = {}
    for pub in jobs.list_publications():
        if pub.youtube_video_id:
            seen.setdefault((pub.project_key, pub.job_id, pub.asset_id), []).append(pub.youtube_video_id)
    return [{"project_key": k[0], "job_id": k[1], "asset_id": k[2], "video_ids": v} for k, v in seen.items() if len(v) > 1]


__all__ = ["FakeYouTubeAdapter", "PublishBlocked", "PublishOutcome", "Publisher", "QuotaReport",
           "RealYouTubeAdapter", "UploadResult", "YouTubeAdapter", "find_duplicate_uploads", "quota_report"]
