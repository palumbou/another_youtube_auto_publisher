"""Glue Lambda for the Step Functions pipeline. Dispatches on event["action"]:

  intake         — S3 object created: create the job record (reads the optional
                   <video>.json sidecar: youtube_id, language, prompt)
  probe_summary  — read work/<job>/probe.json and return what the state machine
                   needs to decide whether to run Transcribe
  finalize       — merge cut_result.json into the job and set PENDING_REVIEW
  fail           — record a pipeline error on the job
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime

import boto3

from autopublisher import storage
from autopublisher.models import STATUS_ERROR, STATUS_PENDING_REVIEW, Job

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_id_for_key(key: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9-]+", "-", key.rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip("-")
    digest = hashlib.sha1(key.encode()).hexdigest()[:8]
    return f"{stem[:60].lower()}-{digest}"


def intake(event) -> dict:
    s3 = boto3.client("s3")
    bucket = event["detail"]["bucket"]["name"]
    key = event["detail"]["object"]["key"]
    if not key.lower().endswith(VIDEO_EXTENSIONS):
        return {"skip": True, "reason": f"not a video: {key}"}

    sidecar = {}
    try:
        body = s3.get_object(Bucket=bucket, Key=f"{key}.json")["Body"].read()
        sidecar = json.loads(body)
    except s3.exceptions.NoSuchKey:
        pass

    job = Job(
        job_id=job_id_for_key(key),
        video_key=key,
        created_at=now_iso(),
        existing_youtube_id=sidecar.get("youtube_id", ""),
        language_override=sidecar.get("language", ""),
        prompt=sidecar.get("prompt", ""),
    )
    storage.save_job(job)
    return {
        "skip": False,
        "job_id": job.job_id,
        "video_key": key,
        "language_override": job.language_override,
        "prompt": job.prompt,
    }


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
    action = event.get("action", "intake")
    return {"intake": intake, "probe_summary": probe_summary,
            "finalize": finalize, "fail": fail}[action](event)
