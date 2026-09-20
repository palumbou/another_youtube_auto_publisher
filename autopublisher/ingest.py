"""READY-driven ingestion: the only way a job enters the system.

A `READY` marker under `incoming/{project_key}/{job_id}/` is the single trigger.
Events may arrive more than once and out of order; the conditional job create
makes the outcome the same. Objects created before `cutover_at` are ignored, so
no pre-existing upload or channel video is ever processed automatically.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from autopublisher import contract
from autopublisher.config import Settings, parse_utc
from autopublisher.contract import ContractError, ContractViolation
from autopublisher.domain import (
    INGESTED,
    AuditEvent,
    Job,
    idempotency_key,
    now_iso,
)
from autopublisher.jobstore import AlreadyExists, JobStore
from autopublisher.media import MediaError, ffprobe
from autopublisher.objectstore import ObjectInfo, ObjectStore, UnsafeKeyError, check_key

JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SOURCE_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm")
ASSET_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".txt", ".json", ".srt", ".vtt")
MANIFEST_NAME = "manifest/video-job-manifest.json"

CREATED = "CREATED"
DUPLICATE = "DUPLICATE"
IGNORED = "IGNORED"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class ReadyEvent:
    bucket: str
    key: str
    version_id: str = ""
    event_time: datetime | None = None
    event_name: str = "ObjectCreated:Put"

    @classmethod
    def from_dict(cls, data: dict) -> ReadyEvent:
        when = data.get("event_time")
        return cls(bucket=data.get("bucket", ""), key=data["key"], version_id=data.get("version_id", ""),
                   event_time=parse_utc(when) if when else None,
                   event_name=data.get("event_name", "ObjectCreated:Put"))

    @classmethod
    def from_eventbridge(cls, event: dict) -> ReadyEvent:
        detail = event.get("detail", {})
        return cls(bucket=detail["bucket"]["name"], key=detail["object"]["key"],
                   version_id=detail["object"].get("version-id", ""),
                   event_time=parse_utc(event["time"]) if event.get("time") else None,
                   event_name=detail.get("reason", "ObjectCreated"))

    @classmethod
    def from_s3_record(cls, record: dict) -> ReadyEvent:
        s3 = record["s3"]
        return cls(bucket=s3["bucket"]["name"], key=s3["object"]["key"],
                   version_id=s3["object"].get("versionId", ""),
                   event_time=parse_utc(record["eventTime"]) if record.get("eventTime") else None,
                   event_name=record.get("eventName", "ObjectCreated:Put"))


@dataclass
class IngestResult:
    status: str
    code: str = ""
    message: str = ""
    job: Job | None = None
    errors: list[ContractError] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"status": self.status, "code": self.code, "message": self.message,
                "job_id": self.job.job_id if self.job else "",
                "errors": [e.as_dict() for e in self.errors]}


@dataclass(frozen=True)
class _DeclaredMedia:
    duration_ms: int
    width: int
    height: int


class Ingestor:
    def __init__(self, settings: Settings, store: ObjectStore, jobs: JobStore, workdir: Path | None = None,
                 probe_media: bool = True):
        self.settings = settings
        self.store = store
        self.jobs = jobs
        self.workdir = workdir
        # Without ffprobe (Lambda) the declared dimensions are recorded and the worker
        # re-validates the media in VALIDATING before anything else happens.
        self.probe_media = probe_media

    # -- helpers -------------------------------------------------------------------

    def _audit(self, project_key: str, job_id: str, action: str, **kw) -> None:
        self.jobs.append_audit(AuditEvent(project_key=project_key, job_id=job_id, action=action,
                                          actor="system:ingest", **kw))

    def _reject(self, project_key: str, job_id: str, errors: list[ContractError]) -> IngestResult:
        self._audit(project_key, job_id, "INGEST_REJECTED", old_state="", new_state="",
                    reason=errors[0].code, details={"errors": [e.as_dict() for e in errors]})
        return IngestResult(REJECTED, errors[0].code, errors[0].message, errors=errors)

    def _ignore(self, code: str, message: str, project_key: str = "", job_id: str = "") -> IngestResult:
        if project_key and job_id:
            self._audit(project_key, job_id, "INGEST_IGNORED", reason=code, details={"message": message})
        return IngestResult(IGNORED, code, message)

    def parse_key(self, key: str) -> tuple[str, str, str] | None:
        """Return (project_key, job_id, relative_path) for a key under the incoming prefix."""
        prefix = self.settings.incoming_prefix
        if not key.startswith(prefix):
            return None
        rest = key[len(prefix):]
        parts = rest.split("/", 2)
        if len(parts) < 3:
            return None
        return parts[0], parts[1], parts[2]

    # -- main entry ----------------------------------------------------------------

    def handle_ready(self, event: ReadyEvent) -> IngestResult:
        try:
            check_key(event.key)
        except UnsafeKeyError as exc:
            return self._ignore(contract.UNSAFE_OBJECT_KEY, str(exc))
        parsed = self.parse_key(event.key)
        if parsed is None or parsed[2] != "READY":
            return self._ignore("NOT_A_READY_MARKER", "only a READY marker under the incoming prefix triggers ingestion")
        project_key, job_id, _ = parsed
        if not PROJECT_RE.match(project_key) or not JOB_ID_RE.match(job_id):
            return self._ignore(contract.UNSAFE_OBJECT_KEY, "project_key or job_id is not path-safe")
        if project_key not in self.settings.allowed_projects:
            return self._reject(project_key, job_id, [ContractError(
                contract.UNSUPPORTED_PROJECT, f"project {project_key} is not enabled", "/project_key")])

        ready = self.store.head(event.key)
        if ready is None:
            return self._ignore(contract.MISSING_READY_DEPENDENCY, "READY object no longer exists",
                                project_key, job_id)
        created_at = ready.last_modified
        if created_at < self.settings.cutover_at:
            return self._ignore(contract.PRE_CUTOVER_JOB,
                                f"READY created {created_at.isoformat()} before cutover "
                                f"{self.settings.cutover_at.isoformat()}", project_key, job_id)

        prefix = f"{self.settings.incoming_prefix}{project_key}/{job_id}/"
        objects = {o.key[len(prefix):]: o for o in self.store.list(prefix)}
        errors = self._layout_errors(objects)
        if errors:
            return self._reject(project_key, job_id, errors)

        source_rel = next(k for k in objects if k.startswith("source/"))
        source = objects[source_rel]
        manifest_required = project_key in self.settings.manifest_required_projects
        manifest_obj = objects.get(MANIFEST_NAME)
        if manifest_obj is None and (manifest_required or not self.settings.allow_unstructured_analysis):
            return self._reject(project_key, job_id, [ContractError(
                contract.MISSING_READY_DEPENDENCY, "manifest/video-job-manifest.json is required", "/manifest")])

        manifest: dict | None = None
        manifest_sha = ""
        if manifest_obj is not None:
            raw = self.store.get_bytes(manifest_obj.key, manifest_obj.version_id)
            manifest_sha = __import__("hashlib").sha256(raw).hexdigest()
            try:
                manifest = contract.validate_manifest(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._reject(project_key, job_id, [ContractError(
                    contract.MANIFEST_SCHEMA_INVALID, f"manifest is not valid JSON: {exc}", "/")])
            except ContractViolation as exc:
                return self._reject(project_key, job_id, exc.errors)
            errors = self._manifest_consistency(manifest, project_key, job_id, source_rel)
            if errors:
                return self._reject(project_key, job_id, errors)

        # Identity: an existing job with the same identity is a duplicate event; a
        # different identity under the same job_id is a changed source.
        source_sha = self.store.sha256(source.key, source.version_id)
        key = idempotency_key(self.settings.environment, project_key, job_id, source_sha, source.version_id)
        existing = self.jobs.get_job(project_key, job_id)
        if existing is not None:
            return self._compare_existing(existing, key, source_sha, source.version_id)

        if manifest is not None and manifest["source"]["sha256"] != source_sha:
            return self._reject(project_key, job_id, [ContractError(
                contract.SOURCE_HASH_MISMATCH, "declared sha256 does not match the uploaded source",
                "/source/sha256")])

        if self.probe_media:
            try:
                info = self._probe(source)
            except MediaError as exc:
                return self._reject(project_key, job_id, [ContractError(contract.INVALID_MEDIA, str(exc), "/source")])
            if manifest is not None:
                errors = self._media_consistency(manifest, info)
                if errors:
                    return self._reject(project_key, job_id, errors)
        elif manifest is not None:
            info = _DeclaredMedia(manifest["source"]["duration_ms"], manifest["source"]["width"], manifest["source"]["height"])
        else:
            info = _DeclaredMedia(0, 0, 0)
        if info.duration_ms > self.settings.max_source_duration_ms:
            return self._reject(project_key, job_id, [ContractError(
                contract.INVALID_MEDIA, "source exceeds the configured duration limit", "/source")])

        job = Job(
            project_key=project_key, job_id=job_id, environment=self.settings.environment,
            idempotency_key=key, source_key=source.key, source_sha256=source_sha,
            source_version_id=source.version_id, ready_version_id=ready.version_id,
            ready_created_at=created_at.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            manifest_key=manifest_obj.key if manifest_obj else "",
            manifest_version_id=manifest_obj.version_id if manifest_obj else "",
            manifest_sha256=manifest_sha,
            channel_profile=manifest["channel_profile"] if manifest else "",
            language=manifest["language"] if manifest else "",
            cutover_decision=f"accepted: READY {created_at.isoformat()} >= cutover "
                             f"{self.settings.cutover_at.isoformat()}",
            state=INGESTED, source_duration_ms=info.duration_ms, source_width=info.width,
            source_height=info.height, summary=manifest["content"]["summary"][:300] if manifest else "",
        )
        try:
            self.jobs.create_job(job)
        except AlreadyExists:
            existing = self.jobs.get_job(project_key, job_id)
            assert existing is not None
            return self._compare_existing(existing, key, source_sha, source.version_id)
        self._audit(project_key, job_id, "INGEST_ACCEPTED", old_state="", new_state=INGESTED,
                    details={"idempotency_key": key, "source_version_id": source.version_id,
                             "ready_version_id": ready.version_id, "cutover": job.cutover_decision})
        return IngestResult(CREATED, "", "job created", job=job)

    # -- checks ------------------------------------------------------------------------

    def _compare_existing(self, existing: Job, key: str, source_sha: str, version_id: str) -> IngestResult:
        if existing.idempotency_key == key:
            return IngestResult(DUPLICATE, contract.DUPLICATE_JOB, "event already ingested", job=existing)
        errors = [ContractError(
            contract.SOURCE_VERSION_CHANGED,
            "an ingested job with this job_id exists with a different source hash or version; "
            "use a new job_id or a contract revision", "/job_id")]
        self._audit(existing.project_key, existing.job_id, "INGEST_REJECTED", reason=errors[0].code,
                    old_state=existing.state, new_state=existing.state,
                    details={"attempted_sha256": source_sha, "attempted_version_id": version_id})
        return IngestResult(REJECTED, errors[0].code, errors[0].message, job=existing, errors=errors)

    def _layout_errors(self, objects: dict[str, ObjectInfo]) -> list[ContractError]:
        errors: list[ContractError] = []
        sources = [k for k in objects if k.startswith("source/")]
        if len(sources) != 1:
            errors.append(ContractError(contract.MISSING_READY_DEPENDENCY,
                                        "exactly one object is expected under source/", "/source"))
        for rel, info in objects.items():
            if rel == "READY":
                continue
            lower = rel.lower()
            if rel.startswith("source/"):
                if not lower.endswith(SOURCE_EXTENSIONS) or rel.count("/") != 1:
                    errors.append(ContractError(contract.INVALID_MEDIA, f"unexpected source object {rel}", "/source"))
                elif info.size > self.settings.max_source_bytes:
                    errors.append(ContractError(contract.INVALID_MEDIA, "source exceeds the size limit", "/source"))
            elif rel == MANIFEST_NAME:
                if info.size > 2 * 1024 * 1024:
                    errors.append(ContractError(contract.MANIFEST_SCHEMA_INVALID, "manifest too large", "/manifest"))
            elif rel.startswith("assets/"):
                if not lower.endswith(ASSET_EXTENSIONS):
                    errors.append(ContractError(contract.INVALID_MEDIA, f"unexpected asset type {rel}", "/assets"))
            elif rel.endswith(".part"):
                errors.append(ContractError(contract.MISSING_READY_DEPENDENCY, f"incomplete upload {rel}", "/"))
            else:
                errors.append(ContractError(contract.UNSAFE_OBJECT_KEY, f"unexpected object {rel}", "/"))
        return errors

    @staticmethod
    def _manifest_consistency(manifest: dict, project_key: str, job_id: str, source_rel: str) -> list[ContractError]:
        errors = []
        if manifest["project_key"] != project_key:
            errors.append(ContractError(contract.UNSUPPORTED_PROJECT, "manifest project_key differs from the prefix",
                                        "/project_key"))
        if manifest["job_id"] != job_id:
            errors.append(ContractError(contract.MANIFEST_SCHEMA_INVALID, "manifest job_id differs from the prefix",
                                        "/job_id"))
        if manifest["source"]["file_name"] != source_rel.split("/", 1)[1]:
            errors.append(ContractError(contract.MANIFEST_SCHEMA_INVALID,
                                        "manifest file_name differs from the uploaded source", "/source/file_name"))
        return errors

    @staticmethod
    def _media_consistency(manifest: dict, info) -> list[ContractError]:
        errors = []
        declared = manifest["source"]
        if abs(declared["duration_ms"] - info.duration_ms) > 500:
            errors.append(ContractError(contract.INVALID_MEDIA,
                                        f"declared duration {declared['duration_ms']} ms differs from probed "
                                        f"{info.duration_ms} ms", "/source/duration_ms"))
        if (declared["width"], declared["height"]) != (info.width, info.height):
            errors.append(ContractError(contract.INVALID_MEDIA, "declared dimensions differ from the media", "/source"))
        if not info.has_audio:
            # Not an error: silence is allowed, but the analysis must know.
            pass
        return errors

    def _probe(self, source: ObjectInfo):
        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
            local = self.store.download(source.key, Path(tmp) / Path(source.key).name, source.version_id)
            return ffprobe(local)


def handle_events(ingestor: Ingestor, events: list[ReadyEvent]) -> list[IngestResult]:
    return [ingestor.handle_ready(e) for e in events]


def utc_now() -> str:
    return now_iso()
