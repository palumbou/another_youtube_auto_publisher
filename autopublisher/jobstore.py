"""Persistence for jobs, revisions, schedules, publications and audit events.

Two implementations share one contract:
- `LocalJobStore`: JSON documents under a directory, guarded by a file lock, with
  the same conditional semantics as DynamoDB (create-if-absent, update-if-version).
- `DynamoJobStore`: one table per entity, conditional expressions for both rules.
"""

from __future__ import annotations

import fcntl
import json
import os
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

from autopublisher.domain import AuditEvent, Job, Publication, Revision, Schedule


class ConflictError(RuntimeError):
    """The stored version differs from the expected one: somebody else changed it."""


class AlreadyExists(RuntimeError):
    """A conditional create found an existing document."""


class NotFound(LookupError):
    pass


class JobStore(ABC):
    # Jobs
    @abstractmethod
    def create_job(self, job: Job) -> None: ...

    @abstractmethod
    def get_job(self, project_key: str, job_id: str) -> Job | None: ...

    @abstractmethod
    def update_job(self, job: Job, expected_version: int) -> None: ...

    @abstractmethod
    def list_jobs(self) -> list[Job]: ...

    # Revisions (immutable once written)
    @abstractmethod
    def put_revision(self, revision: Revision) -> None: ...

    @abstractmethod
    def get_revision(self, project_key: str, job_id: str, number: int) -> Revision | None: ...

    @abstractmethod
    def update_revision_review(self, revision: Revision) -> None:
        """Only review fields change on a revision; the processing output stays immutable."""

    @abstractmethod
    def list_revisions(self, project_key: str, job_id: str) -> list[Revision]: ...

    # Schedules / publications
    @abstractmethod
    def create_schedule(self, schedule: Schedule) -> None: ...

    @abstractmethod
    def update_schedule(self, schedule: Schedule, expected_version: int) -> None: ...

    @abstractmethod
    def list_schedules(self, status: str | None = None) -> list[Schedule]: ...

    @abstractmethod
    def create_publication(self, publication: Publication) -> None: ...

    @abstractmethod
    def get_publication(self, publication_id: str) -> Publication | None: ...

    @abstractmethod
    def update_publication(self, publication: Publication, expected_version: int) -> None: ...

    @abstractmethod
    def list_publications(self) -> list[Publication]: ...

    # Audit
    @abstractmethod
    def append_audit(self, event: AuditEvent) -> AuditEvent: ...

    @abstractmethod
    def list_audit(self, project_key: str, job_id: str) -> list[AuditEvent]: ...


# --- Local JSON implementation ------------------------------------------------------

class LocalJobStore(JobStore):
    def __init__(self, root: Path):
        self.root = Path(root)
        for sub in ("jobs", "revisions", "schedules", "publications", "audit"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".lock"

    @contextmanager
    def _lock(self):
        with open(self._lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _safe(name: str) -> str:
        return name.replace("/", "_").replace("#", "__")

    def _write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(tmp, path)

    def _read(self, path: Path) -> dict | None:
        return json.loads(path.read_text()) if path.is_file() else None

    # jobs
    def _job_path(self, project_key: str, job_id: str) -> Path:
        return self.root / "jobs" / f"{self._safe(project_key)}__{self._safe(job_id)}.json"

    def create_job(self, job: Job) -> None:
        with self._lock():
            path = self._job_path(job.project_key, job.job_id)
            if path.exists():
                raise AlreadyExists(job.pk)
            self._write(path, job.to_dict())

    def get_job(self, project_key: str, job_id: str) -> Job | None:
        data = self._read(self._job_path(project_key, job_id))
        return Job.from_dict(data) if data else None

    def update_job(self, job: Job, expected_version: int) -> None:
        with self._lock():
            path = self._job_path(job.project_key, job.job_id)
            current = self._read(path)
            if current is None:
                raise NotFound(job.pk)
            if current["version"] != expected_version:
                raise ConflictError(f"{job.pk}: expected version {expected_version}, found {current['version']}")
            job.version = expected_version + 1
            self._write(path, job.to_dict())

    def list_jobs(self) -> list[Job]:
        jobs = [Job.from_dict(json.loads(p.read_text())) for p in sorted((self.root / "jobs").glob("*.json"))]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    # revisions
    def _rev_path(self, project_key: str, job_id: str, number: int) -> Path:
        return self.root / "revisions" / f"{self._safe(project_key)}__{self._safe(job_id)}__{number:04d}.json"

    def put_revision(self, revision: Revision) -> None:
        with self._lock():
            path = self._rev_path(revision.project_key, revision.job_id, revision.number)
            if path.exists():
                raise AlreadyExists(f"revision {revision.number} of {revision.job_id}")
            self._write(path, revision.to_dict())

    def get_revision(self, project_key: str, job_id: str, number: int) -> Revision | None:
        data = self._read(self._rev_path(project_key, job_id, number))
        return Revision.from_dict(data) if data else None

    def update_revision_review(self, revision: Revision) -> None:
        with self._lock():
            path = self._rev_path(revision.project_key, revision.job_id, revision.number)
            current = self._read(path)
            if current is None:
                raise NotFound(f"revision {revision.number} of {revision.job_id}")
            fresh = revision.to_dict()
            for key in ("master_review_status", "master_review_reason", "master_metadata", "reject_reason"):
                current[key] = fresh[key]
            for stored, new in zip(current["shorts"], fresh["shorts"], strict=True):
                for key in ("review_status", "review_reason", "metadata"):
                    stored[key] = new[key]
            if current.get("master") and fresh.get("master"):
                for key in ("review_status", "review_reason", "metadata"):
                    current["master"][key] = fresh["master"][key]
            self._write(path, current)

    def list_revisions(self, project_key: str, job_id: str) -> list[Revision]:
        prefix = f"{self._safe(project_key)}__{self._safe(job_id)}__"
        paths = sorted((self.root / "revisions").glob(prefix + "*.json"))
        return [Revision.from_dict(json.loads(p.read_text())) for p in paths]

    # schedules
    def create_schedule(self, schedule: Schedule) -> None:
        with self._lock():
            path = self.root / "schedules" / f"{self._safe(schedule.schedule_id)}.json"
            if path.exists():
                raise AlreadyExists(schedule.schedule_id)
            self._write(path, schedule.to_dict())

    def update_schedule(self, schedule: Schedule, expected_version: int) -> None:
        with self._lock():
            path = self.root / "schedules" / f"{self._safe(schedule.schedule_id)}.json"
            current = self._read(path)
            if current is None:
                raise NotFound(schedule.schedule_id)
            if current["version"] != expected_version:
                raise ConflictError(schedule.schedule_id)
            schedule.version = expected_version + 1
            self._write(path, schedule.to_dict())

    def list_schedules(self, status: str | None = None) -> list[Schedule]:
        out = [Schedule.from_dict(json.loads(p.read_text())) for p in sorted((self.root / "schedules").glob("*.json"))]
        return [s for s in out if status is None or s.status == status]

    # publications
    def create_publication(self, publication: Publication) -> None:
        with self._lock():
            path = self.root / "publications" / f"{self._safe(publication.publication_id)}.json"
            if path.exists():
                raise AlreadyExists(publication.publication_id)
            self._write(path, publication.to_dict())

    def get_publication(self, publication_id: str) -> Publication | None:
        data = self._read(self.root / "publications" / f"{self._safe(publication_id)}.json")
        return Publication.from_dict(data) if data else None

    def update_publication(self, publication: Publication, expected_version: int) -> None:
        with self._lock():
            path = self.root / "publications" / f"{self._safe(publication.publication_id)}.json"
            current = self._read(path)
            if current is None:
                raise NotFound(publication.publication_id)
            if current["version"] != expected_version:
                raise ConflictError(publication.publication_id)
            publication.version = expected_version + 1
            self._write(path, publication.to_dict())

    def list_publications(self) -> list[Publication]:
        return [Publication.from_dict(json.loads(p.read_text()))
                for p in sorted((self.root / "publications").glob("*.json"))]

    # audit
    def append_audit(self, event: AuditEvent) -> AuditEvent:
        with self._lock():
            path = self.root / "audit" / f"{self._safe(event.project_key)}__{self._safe(event.job_id)}.jsonl"
            count = len(path.read_text().splitlines()) if path.exists() else 0
            event.sequence = count + 1
            with open(path, "a") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def list_audit(self, project_key: str, job_id: str) -> list[AuditEvent]:
        path = self.root / "audit" / f"{self._safe(project_key)}__{self._safe(job_id)}.jsonl"
        if not path.exists():
            return []
        return [AuditEvent.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line]


# --- DynamoDB implementation ----------------------------------------------------------

class DynamoJobStore(JobStore):
    """Tables: jobs (pk), revisions (pk, number), schedules (schedule_id),
    publications (publication_id), audit (pk, sequence)."""

    def __init__(self, table_prefix: str, resource=None):
        import boto3

        self.ddb = resource or boto3.resource("dynamodb")
        self.jobs = self.ddb.Table(f"{table_prefix}-jobs")
        self.revisions = self.ddb.Table(f"{table_prefix}-revisions")
        self.schedules = self.ddb.Table(f"{table_prefix}-schedules")
        self.publications = self.ddb.Table(f"{table_prefix}-publications")
        self.audit = self.ddb.Table(f"{table_prefix}-audit")

    @staticmethod
    def _dyn(value):
        from decimal import Decimal

        return json.loads(json.dumps(value), parse_float=Decimal)

    @staticmethod
    def _py(value):
        from decimal import Decimal

        return json.loads(json.dumps(value, default=lambda d: float(d) if isinstance(d, Decimal) else str(d)))

    def _conditional_put(self, table, item: dict, key_attr: str):
        try:
            table.put_item(Item=self._dyn(item), ConditionExpression=f"attribute_not_exists({key_attr})")
        except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
            raise AlreadyExists(item[key_attr]) from exc

    def _versioned_put(self, table, item: dict, expected_version: int, name: str):
        item["version"] = expected_version + 1
        try:
            table.put_item(Item=self._dyn(item), ConditionExpression="version = :v",
                           ExpressionAttributeValues={":v": expected_version})
        except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
            raise ConflictError(name) from exc

    def create_job(self, job: Job) -> None:
        item = job.to_dict() | {"pk": job.pk}
        self._conditional_put(self.jobs, item, "pk")

    def get_job(self, project_key: str, job_id: str) -> Job | None:
        item = self.jobs.get_item(Key={"pk": f"{project_key}#{job_id}"}).get("Item")
        return Job.from_dict(self._py(item)) if item else None

    def update_job(self, job: Job, expected_version: int) -> None:
        item = job.to_dict() | {"pk": job.pk}
        self._versioned_put(self.jobs, item, expected_version, job.pk)
        job.version = expected_version + 1

    def list_jobs(self) -> list[Job]:
        items, kwargs = [], {}
        while True:
            page = self.jobs.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        jobs = [Job.from_dict(self._py(i)) for i in items]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def put_revision(self, revision: Revision) -> None:
        item = revision.to_dict() | {"pk": f"{revision.project_key}#{revision.job_id}"}
        try:
            self.revisions.put_item(Item=self._dyn(item),
                                    ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(#n)",
                                    ExpressionAttributeNames={"#n": "number"})
        except self.revisions.meta.client.exceptions.ConditionalCheckFailedException as exc:
            raise AlreadyExists(f"revision {revision.number}") from exc

    def get_revision(self, project_key: str, job_id: str, number: int) -> Revision | None:
        item = self.revisions.get_item(Key={"pk": f"{project_key}#{job_id}", "number": number}).get("Item")
        return Revision.from_dict(self._py(item)) if item else None

    def update_revision_review(self, revision: Revision) -> None:
        self.revisions.update_item(
            Key={"pk": f"{revision.project_key}#{revision.job_id}", "number": revision.number},
            UpdateExpression="SET master_review_status=:a, master_review_reason=:b, master_metadata=:c, "
                             "reject_reason=:d, shorts=:e, master=:f",
            ExpressionAttributeValues=self._dyn({
                ":a": revision.master_review_status, ":b": revision.master_review_reason,
                ":c": revision.master_metadata, ":d": revision.reject_reason,
                ":e": [a.__dict__ for a in revision.shorts],
                ":f": revision.master.__dict__ if revision.master else None,
            }),
        )

    def list_revisions(self, project_key: str, job_id: str) -> list[Revision]:
        from boto3.dynamodb.conditions import Key

        page = self.revisions.query(KeyConditionExpression=Key("pk").eq(f"{project_key}#{job_id}"))
        return [Revision.from_dict(self._py(i)) for i in page.get("Items", [])]

    def create_schedule(self, schedule: Schedule) -> None:
        self._conditional_put(self.schedules, schedule.to_dict(), "schedule_id")

    def update_schedule(self, schedule: Schedule, expected_version: int) -> None:
        self._versioned_put(self.schedules, schedule.to_dict(), expected_version, schedule.schedule_id)
        schedule.version = expected_version + 1

    def list_schedules(self, status: str | None = None) -> list[Schedule]:
        items = self.schedules.scan().get("Items", [])
        out = [Schedule.from_dict(self._py(i)) for i in items]
        return [s for s in out if status is None or s.status == status]

    def create_publication(self, publication: Publication) -> None:
        self._conditional_put(self.publications, publication.to_dict(), "publication_id")

    def get_publication(self, publication_id: str) -> Publication | None:
        item = self.publications.get_item(Key={"publication_id": publication_id}).get("Item")
        return Publication.from_dict(self._py(item)) if item else None

    def update_publication(self, publication: Publication, expected_version: int) -> None:
        self._versioned_put(self.publications, publication.to_dict(), expected_version,
                            publication.publication_id)
        publication.version = expected_version + 1

    def list_publications(self) -> list[Publication]:
        return [Publication.from_dict(self._py(i)) for i in self.publications.scan().get("Items", [])]

    def append_audit(self, event: AuditEvent) -> AuditEvent:
        from boto3.dynamodb.conditions import Key

        pk = f"{event.project_key}#{event.job_id}"
        last = self.audit.query(KeyConditionExpression=Key("pk").eq(pk), ScanIndexForward=False, Limit=1)
        items = last.get("Items", [])
        event.sequence = int(items[0]["sequence"]) + 1 if items else 1
        self.audit.put_item(Item=self._dyn(event.to_dict() | {"pk": pk}),
                            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(#s)",
                            ExpressionAttributeNames={"#s": "sequence"})
        return event

    def list_audit(self, project_key: str, job_id: str) -> list[AuditEvent]:
        from boto3.dynamodb.conditions import Key

        page = self.audit.query(KeyConditionExpression=Key("pk").eq(f"{project_key}#{job_id}"))
        return [AuditEvent.from_dict(self._py(i)) for i in page.get("Items", [])]
