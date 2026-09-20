"""AWS Lambda entry points. Dispatch on event["action"] or on the event shape:

  ingest    — SQS batch of S3/EventBridge object-created events; every READY marker
              goes through the contract-driven ingestor and each CREATED job starts
              one Step Functions execution (the Fargate worker runs the revision)
  reprocess — from the console: start an execution for a REPROCESS_REQUESTED job
  publish   — EventBridge Scheduler: run due schedules through the configured adapter
  console   — API Gateway (JWT-authorized) request for the review console
  fail      — state machine catch: mark the job FAILED when the worker task died

The former "intake" created a job for any video object and read a sidecar naming a
youtube_id to publish Shorts for a video already on the channel; both are retired.
Only post-cutover READY jobs are eligible and the channel is never crawled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from autopublisher.config import Settings
from autopublisher.console import (
    Console,
    GatewayJwtAuthenticator,
    request_from_lambda,
    response_to_lambda,
)
from autopublisher.domain import FAILED, FULL, AuditEvent, Job
from autopublisher.ingest import CREATED, Ingestor, ReadyEvent
from autopublisher.jobstore import DynamoJobStore
from autopublisher.objectstore import S3ObjectStore
from autopublisher.publishing import FakeYouTubeAdapter, Publisher, RealYouTubeAdapter
from autopublisher.review import ReviewService


def ready_events_from_records(event: dict) -> list[ReadyEvent]:
    """Accept an SQS batch (bodies are EventBridge or S3 notifications), a bare
    EventBridge event, or a bare S3 notification. Non-READY keys are filtered later."""
    out: list[ReadyEvent] = []
    records = event.get("Records")
    if records is None:
        if "detail" in event:
            return [ReadyEvent.from_eventbridge(event)]
        return out
    for record in records:
        body = record.get("body")
        payload = json.loads(body) if isinstance(body, str) else record
        if "detail" in payload:
            out.append(ReadyEvent.from_eventbridge(payload))
        elif "Records" in payload:
            out.extend(ReadyEvent.from_s3_record(r) for r in payload["Records"] if "s3" in r)
        elif "s3" in payload:
            out.append(ReadyEvent.from_s3_record(payload))
    return out


def _stores():
    return S3ObjectStore(os.environ["BUCKET"]), DynamoJobStore(os.environ["TABLE_PREFIX"])


def start_execution(job: Job, scope: str, reason: str = "") -> None:
    import boto3

    boto3.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=f"{job.job_id}-r{job.current_revision + 1}"[:80],
        input=json.dumps({"project_key": job.project_key, "job_id": job.job_id, "scope": scope, "reason": reason[:500]}),
    )


def ingest(event, settings: Settings | None = None) -> dict:
    settings = settings or Settings.from_env()
    store, jobs = _stores()
    ingestor = Ingestor(settings, store, jobs, workdir=Path("/tmp"),
                        probe_media=os.environ.get("INGEST_PROBE_MEDIA", "0") == "1")
    results, failures = [], []
    records = event.get("Records") or [event]
    for record in records:
        try:
            for ready in ready_events_from_records({"Records": [record]} if "body" in record else record):
                result = ingestor.handle_ready(ready)
                if result.status == CREATED and result.job:
                    start_execution(result.job, FULL)
                results.append(result.as_dict())
        except Exception as exc:  # noqa: BLE001 - report the record to SQS, let the rest of the batch succeed
            failures.append({"itemIdentifier": record.get("messageId", "")})
            results.append({"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"[:300]})
    return {"results": results, "batchItemFailures": failures}


def fail(event, settings: Settings | None = None) -> dict:
    """Called by the state machine when the worker task itself failed."""
    _, jobs = _stores()
    job = jobs.get_job(event["project_key"], event["job_id"])
    if job is None:
        return {"error": "job not found"}
    if job.can_transition(FAILED):
        old = job.transition(FAILED)
        job.error = {"code": "WORKER_FAILED", "message": json.dumps(event.get("error", ""))[:1000]}
        jobs.update_job(job, expected_version=job.version)
        jobs.append_audit(AuditEvent(project_key=job.project_key, job_id=job.job_id, action="STATE", actor="system:statemachine",
                                     old_state=old, new_state=FAILED, revision=job.current_revision, reason="WORKER_FAILED"))
    return {"job": job.pk, "state": job.state}


def reprocess(event, settings: Settings | None = None) -> dict:
    _, jobs = _stores()
    job = jobs.get_job(event["project_key"], event["job_id"])
    if job is None:
        return {"error": "job not found"}
    start_execution(job, event.get("scope", FULL), event.get("reason", ""))
    return {"started": job.pk}


def publish(event, settings: Settings | None = None) -> dict:
    settings = settings or Settings.from_env()
    store, jobs = _stores()
    if settings.youtube_mode == "real":
        import boto3

        raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["YOUTUBE_SECRET"])["SecretString"]
        adapter = RealYouTubeAdapter.from_secret(raw)
    else:
        adapter = FakeYouTubeAdapter()
    outcomes = Publisher(settings, store, jobs, adapter, workdir=Path("/tmp")).run_due()
    return {"outcomes": [o.__dict__ for o in outcomes]}


def console(event, settings: Settings | None = None) -> dict:
    settings = settings or Settings.from_env()
    store, jobs = _stores()
    allowed = tuple(v for v in os.environ.get("CONSOLE_ALLOWED_USERS", "").split(",") if v)
    app = Console(settings, jobs, store, ReviewService(settings, jobs), GatewayJwtAuthenticator(allowed),
                  on_reprocess=lambda job, scope, reason: start_execution(job, scope, reason))
    return response_to_lambda(app.handle(request_from_lambda(event)))


def handler(event, context):
    action = event.get("action") if isinstance(event, dict) else None
    if not action:
        if "requestContext" in event:
            action = "console"
        elif "Records" in event or "detail" in event:
            action = "ingest"
    return {"ingest": ingest, "reprocess": reprocess, "publish": publish, "console": console, "fail": fail}[action](event)
