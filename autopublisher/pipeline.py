"""Glue Lambda for the Step Functions pipeline. Dispatches on event["action"]:

  ingest         — SQS batch of S3/EventBridge object-created events: every READY
                   marker goes through the contract-driven ingestor (autopublisher.ingest)
  probe_summary  — read work/<job>/probe.json and return what the state machine
                   needs to decide whether to run Transcribe
  finalize       — merge cut_result.json into the job and set PENDING_REVIEW
  fail           — record a pipeline error on the job

The former "intake" action created a job for any video object and read an optional
sidecar with a `youtube_id` to publish Shorts for a video already on the channel.
Both are retired: only post-cutover READY jobs are eligible, and the channel is
never crawled or backfilled.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import boto3

from autopublisher import storage
from autopublisher.config import Settings
from autopublisher.ingest import Ingestor, ReadyEvent
from autopublisher.jobstore import DynamoJobStore
from autopublisher.models import STATUS_ERROR, STATUS_PENDING_REVIEW
from autopublisher.objectstore import S3ObjectStore


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def build_ingestor(settings: Settings | None = None) -> Ingestor:
    settings = settings or Settings.from_env()
    return Ingestor(settings, S3ObjectStore(os.environ["BUCKET"]),
                    DynamoJobStore(os.environ["TABLE_PREFIX"]), workdir=Path("/tmp"))


def ingest(event) -> dict:
    ingestor = build_ingestor()
    results = [ingestor.handle_ready(e) for e in ready_events_from_records(event)]
    return {"results": [r.as_dict() for r in results]}


def probe_summary(event) -> dict:
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    job_id = event["job_id"]
    prefix = f"work/{job_id}"
    probe = json.loads(s3.get_object(Bucket=bucket, Key=f"{prefix}/probe.json")["Body"].read())
    language = event.get("language_override") or ""
    return {
        "job_id": job_id,
        "has_speech": bool(probe.get("has_speech")) and bool(probe.get("audio_file")),
        "audio_uri": f"s3://{bucket}/{prefix}/{probe['audio_file']}" if probe.get("audio_file") else "",
        "transcript_key": f"{prefix}/transcript.json",
        "language": language,
        "identify_language": not language,
    }


def finalize(event) -> dict:
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    job_id = event["job_id"]
    job = storage.load_job(job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")

    result = json.loads(
        s3.get_object(Bucket=bucket, Key=f"work/{job_id}/cut_result.json")["Body"].read()
    )
    keys = {item["short_id"]: item["s3_key"] for item in result["shorts"]}
    for short in job.shorts:
        short.s3_key = keys.get(short.short_id, short.s3_key)

    job.error = ""
    job.status = STATUS_PENDING_REVIEW
    storage.save_job(job)
    return {"job_id": job_id, "status": job.status, "num_shorts": len(job.shorts)}


def fail(event) -> dict:
    job_id = event.get("job_id")
    job = storage.load_job(job_id) if job_id else None
    if job:
        job.status = STATUS_ERROR
        job.error = json.dumps(event.get("error", ""))[:2000]
        storage.save_job(job)
    return {"job_id": job_id, "status": STATUS_ERROR}


def handler(event, context):
    action = event.get("action") or ("ingest" if "Records" in event or "detail" in event else "")
    return {"ingest": ingest, "probe_summary": probe_summary,
            "finalize": finalize, "fail": fail}[action](event)
