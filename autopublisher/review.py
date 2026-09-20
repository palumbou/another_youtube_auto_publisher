"""Review actions: the only way a job moves past AWAITING_REVIEW. Every action is
version-checked, audited with actor and source IP, and never uploads anything."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from autopublisher.config import Settings, parse_utc
from autopublisher.domain import (
    APPROVED,
    AWAITING_REVIEW,
    CANCELLED,
    FAILED,
    PENDING,
    REPROCESS_REQUESTED,
    REVIEW_APPROVED,
    REVIEW_REJECTED,
    SCHEDULED,
    SCOPES,
    UNPUBLISHED_STATES,
    AuditEvent,
    InvalidTransition,
    Job,
    Revision,
    Schedule,
)
from autopublisher.jobstore import ConflictError, JobStore

EDITABLE_FIELDS = ("title", "description", "chapters", "hashtags", "tags", "category", "playlist",
                   "default_language", "made_for_kids", "contains_synthetic_media", "notify_subscribers",
                   "owner_confirmed_audience", "owner_confirmed_synthetic")
MAX_TITLE, MAX_DESCRIPTION = 100, 5000


class ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    name: str
    source_ip: str = ""


class ReviewService:
    def __init__(self, settings: Settings, jobs: JobStore):
        self.settings, self.jobs = settings, jobs

    # -- helpers ------------------------------------------------------------------

    def _load(self, project_key: str, job_id: str) -> tuple[Job, Revision]:
        job = self.jobs.get_job(project_key, job_id)
        if job is None:
            raise ReviewError("job not found")
        revision = self.jobs.get_revision(project_key, job_id, job.current_revision) if job.current_revision else None
        if revision is None:
            raise ReviewError("job has no revision to review yet")
        return job, revision

    def _check_version(self, job: Job, expected_version: int | None) -> None:
        if expected_version is not None and job.version != expected_version:
            raise ConflictError(f"{job.pk}: the job changed since you loaded it (version {job.version})")

    def _audit(self, job: Job, actor: Actor, action: str, old_state: str, reason: str = "", **details) -> None:
        self.jobs.append_audit(AuditEvent(project_key=job.project_key, job_id=job.job_id, action=action,
                                          actor=actor.name, source_ip=actor.source_ip, old_state=old_state,
                                          new_state=job.state, revision=job.current_revision, reason=reason,
                                          details=details))

    def _require_review_state(self, job: Job) -> None:
        if job.state != AWAITING_REVIEW:
            raise ReviewError(f"job is {job.state}, not {AWAITING_REVIEW}")

    @staticmethod
    def _validate_metadata(meta: dict) -> list[str]:
        problems = []
        if not meta.get("title") or len(meta["title"]) > MAX_TITLE or "<" in meta["title"] or ">" in meta["title"]:
            problems.append("title is required, at most 100 characters, without angle brackets")
        if len(meta.get("description", "")) > MAX_DESCRIPTION:
            problems.append("description exceeds 5000 characters")
        hashtags = meta.get("hashtags", [])
        if not 1 <= len(hashtags) <= 3 or any(not h.startswith("#") or " " in h for h in hashtags):
            problems.append("one to three hashtags are required")
        if len(meta.get("tags", [])) > 15:
            problems.append("at most 15 tags")
        description_lower = meta.get("description", "").lower()
        if any(t.lower() in description_lower for t in meta.get("tags", []) if len(t) > 3) and len(meta.get("tags", [])) > 5:
            problems.append("tags must not be repeated inside the description")
        return problems

    # -- metadata -------------------------------------------------------------------

    def update_metadata(self, project_key: str, job_id: str, asset_id: str, changes: dict, actor: Actor,
                        expected_version: int | None = None) -> Revision:
        job, revision = self._load(project_key, job_id)
        self._require_review_state(job)
        self._check_version(job, expected_version)
        target = revision.master_metadata if asset_id == "master" else None
        if target is None:
            asset = revision.asset(asset_id)
            if asset is None:
                raise ReviewError(f"unknown asset {asset_id}")
            target = asset.metadata
        unknown = set(changes) - set(EDITABLE_FIELDS)
        if unknown:
            raise ReviewError(f"fields cannot be edited: {sorted(unknown)}")
        before = {k: target.get(k) for k in changes}
        target.update(changes)
        problems = self._validate_metadata(target)
        if problems:
            target.update(before)
            raise ReviewError("; ".join(problems))
        self.jobs.update_revision_review(revision)
        self.jobs.update_job(job, expected_version=job.version)  # bump version: an edit is a change
        self._audit(job, actor, "METADATA_EDITED", job.state, asset_id=asset_id, changed=sorted(changes))
        return revision

    # -- approvals ------------------------------------------------------------------------

    def _confirmations_ok(self, meta: dict) -> None:
        if not meta.get("owner_confirmed_audience"):
            raise ReviewError("confirm the made-for-kids decision before approving")
        if meta.get("contains_synthetic_media") and not meta.get("owner_confirmed_synthetic"):
            raise ReviewError("confirm the synthetic-media declaration before approving")
        problems = self._validate_metadata(meta)
        if problems:
            raise ReviewError("; ".join(problems))

    def approve_master(self, project_key: str, job_id: str, actor: Actor, expected_version: int | None = None) -> Revision:
        job, revision = self._load(project_key, job_id)
        self._require_review_state(job)
        self._check_version(job, expected_version)
        self._confirmations_ok(revision.master_metadata)
        revision.master_review_status = REVIEW_APPROVED
        revision.master_review_reason = ""
        if revision.master:
            revision.master.review_status = REVIEW_APPROVED
        self.jobs.update_revision_review(revision)
        self.jobs.update_job(job, expected_version=job.version)
        self._audit(job, actor, "MASTER_APPROVED", job.state)
        return revision

    def review_short(self, project_key: str, job_id: str, asset_id: str, approve: bool, actor: Actor,
                     reason: str = "", expected_version: int | None = None) -> Revision:
        job, revision = self._load(project_key, job_id)
        self._require_review_state(job)
        self._check_version(job, expected_version)
        asset = next((a for a in revision.shorts if a.asset_id == asset_id), None)
        if asset is None:
            raise ReviewError(f"unknown short {asset_id}")
        if approve:
            if not asset.quality.get("ok", True):
                raise ReviewError("this short failed the automated quality checks; reprocess or reject it")
            self._confirmations_ok(asset.metadata)
            asset.review_status, asset.review_reason = REVIEW_APPROVED, ""
        else:
            if not reason.strip():
                raise ReviewError("a rejection needs a reason")
            asset.review_status, asset.review_reason = REVIEW_REJECTED, reason.strip()
        self.jobs.update_revision_review(revision)
        self.jobs.update_job(job, expected_version=job.version)
        self._audit(job, actor, "SHORT_APPROVED" if approve else "SHORT_REJECTED", job.state, reason=reason,
                    asset_id=asset_id)
        return revision

    def approve_selected(self, project_key: str, job_id: str, actor: Actor, include_master: bool,
                         short_ids: list[str], expected_version: int | None = None) -> Job:
        """Approve a batch and move the job to APPROVED. Unselected shorts stay pending
        and are never uploaded."""
        job, revision = self._load(project_key, job_id)
        self._require_review_state(job)
        self._check_version(job, expected_version)
        if include_master:
            self._confirmations_ok(revision.master_metadata)
            revision.master_review_status = REVIEW_APPROVED
            if revision.master:
                revision.master.review_status = REVIEW_APPROVED
        for asset in revision.shorts:
            if asset.asset_id in short_ids:
                if not asset.quality.get("ok", True):
                    raise ReviewError(f"{asset.asset_id} failed the automated quality checks")
                self._confirmations_ok(asset.metadata)
                asset.review_status, asset.review_reason = REVIEW_APPROVED, ""
        if not revision.approved_assets():
            raise ReviewError("nothing selected for approval")
        self.jobs.update_revision_review(revision)
        old = job.transition(APPROVED)
        self.jobs.update_job(job, expected_version=job.version)
        self._audit(job, actor, "BATCH_APPROVED", old, include_master=include_master, shorts=sorted(short_ids))
        return job

    # -- rejection / reprocess / cancel -------------------------------------------------------

    def reject(self, project_key: str, job_id: str, reason: str, scope: str, actor: Actor,
               expected_version: int | None = None) -> Job:
        job, revision = self._load(project_key, job_id)
        if job.state not in (AWAITING_REVIEW, FAILED):
            raise ReviewError(f"cannot reject a job in state {job.state}")
        self._check_version(job, expected_version)
        if not reason.strip():
            raise ReviewError("a rejection needs a reason")
        if scope not in SCOPES:
            raise ReviewError(f"scope must be one of {', '.join(SCOPES)}")
        revision.reject_reason = reason.strip()
        revision.master_review_status = REVIEW_REJECTED
        self.jobs.update_revision_review(revision)
        old = job.transition(REPROCESS_REQUESTED)
        job.reprocess_scope = scope
        job.error = {"owner_notes": reason.strip()[:2000]}
        self.jobs.update_job(job, expected_version=job.version)
        self._audit(job, actor, "REJECTED", old, reason=reason, scope=scope)
        return job

    def cancel(self, project_key: str, job_id: str, reason: str, actor: Actor,
               expected_version: int | None = None) -> Job:
        job = self.jobs.get_job(project_key, job_id)
        if job is None:
            raise ReviewError("job not found")
        self._check_version(job, expected_version)
        if job.state not in UNPUBLISHED_STATES or not job.can_transition(CANCELLED):
            raise ReviewError(f"cannot cancel a job in state {job.state}")
        if not reason.strip():
            raise ReviewError("a cancellation needs a reason")
        old = job.transition(CANCELLED)
        self.jobs.update_job(job, expected_version=job.version)
        for schedule in self.jobs.list_schedules():
            if schedule.job_id == job_id and schedule.project_key == project_key and schedule.status in ("PENDING", "QUEUED"):
                schedule.status = "CANCELLED"
                self.jobs.update_schedule(schedule, expected_version=schedule.version)
        self._audit(job, actor, "CANCELLED", old, reason=reason)
        return job

    # -- scheduling ---------------------------------------------------------------------------------

    def schedule(self, project_key: str, job_id: str, asset_id: str, publish_at_local: str, actor: Actor,
                 notify_subscribers: bool = False, expected_version: int | None = None) -> Schedule:
        """publish_at_local is an ISO timestamp in the owner timezone (or with an explicit offset)."""
        job, revision = self._load(project_key, job_id)
        if job.state not in (APPROVED, SCHEDULED):
            raise ReviewError(f"schedule needs an approved job, not {job.state}")
        self._check_version(job, expected_version)
        asset = revision.asset(asset_id)
        if asset is None or asset.review_status != REVIEW_APPROVED:
            raise ReviewError(f"{asset_id} is not an approved asset of revision {revision.number}")
        when = self._to_utc(publish_at_local)
        if when <= datetime.now(UTC):
            raise ReviewError("publish time must be in the future")
        schedule_id = hashlib.sha256(f"{project_key}#{job_id}#{revision.number}#{asset_id}".encode()).hexdigest()[:24]
        existing = next((s for s in self.jobs.list_schedules() if s.schedule_id == schedule_id), None)
        publish_at = when.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if existing:
            if existing.status not in ("PENDING", "QUEUED"):
                raise ReviewError(f"schedule already {existing.status}")
            existing.publish_at_utc, existing.notify_subscribers = publish_at, notify_subscribers
            existing.status = "PENDING"
            self.jobs.update_schedule(existing, expected_version=existing.version)
            schedule = existing
            action = "RESCHEDULED"
        else:
            schedule = Schedule(schedule_id=schedule_id, project_key=project_key, job_id=job_id,
                                revision=revision.number, asset_id=asset_id, publish_at_utc=publish_at,
                                owner_timezone=self.settings.owner_timezone, notify_subscribers=notify_subscribers)
            self.jobs.create_schedule(schedule)
            action = "SCHEDULED"
        if job.state == APPROVED:
            old = job.transition(SCHEDULED)
        else:
            old = job.state
        self.jobs.update_job(job, expected_version=job.version)
        self._audit(job, actor, action, old, asset_id=asset_id, publish_at_utc=publish_at,
                    owner_timezone=self.settings.owner_timezone)
        return schedule

    def _to_utc(self, value: str) -> datetime:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise ReviewError("publish time must be ISO 8601") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self.settings.owner_timezone))
        return parsed.astimezone(UTC)

    # -- read model ---------------------------------------------------------------------------------

    def read_model(self, project_key: str, job_id: str) -> dict:
        job = self.jobs.get_job(project_key, job_id)
        if job is None:
            raise ReviewError("job not found")
        revision = self.jobs.get_revision(project_key, job_id, job.current_revision) if job.current_revision else None
        return {
            "project_key": job.project_key, "job_id": job.job_id, "state": job.state,
            "current_revision": job.current_revision,
            "master_review_status": revision.master_review_status if revision else PENDING,
            "shorts": [{"candidate_id": a.asset_id, "review_status": a.review_status} for a in revision.shorts] if revision else [],
            "updated_at": job.updated_at,
        }


def local_display(publish_at_utc: str, timezone: str) -> str:
    try:
        return parse_utc(publish_at_utc).astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, KeyError):
        return publish_at_utc


__all__ = ["Actor", "InvalidTransition", "ReviewError", "ReviewService", "local_display"]
