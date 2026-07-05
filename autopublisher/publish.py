"""Scheduled publisher Lambda (EventBridge, default: daily).

Each run:
  1. moves APPROVED jobs into PUBLISHING — uploading the main video as unlisted
     first, unless the job references a pre-existing YouTube video;
  2. publishes exactly ONE Short across all publishing jobs (oldest job first,
     in segment order) to keep a steady channel cadence;
  3. completes jobs whose Shorts are all live: the main video goes public and
     the job moves to DONE.

Videos stream from S3 straight into the YouTube resumable upload, so the only
practical size bound is the Lambda timeout, not /tmp space.

Env: BUCKET, JOBS_TABLE, YOUTUBE_SECRET (Secrets Manager name/ARN holding JSON
with client_id, client_secret, refresh_token).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import boto3

from autopublisher import storage
from autopublisher.models import (
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PUBLISHING,
    Job,
    next_short_to_publish,
    short_description_with_link,
)
from autopublisher.youtube import YouTubeClient


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_client() -> YouTubeClient:
    secrets = boto3.client("secretsmanager")
    raw = secrets.get_secret_value(SecretId=os.environ["YOUTUBE_SECRET"])["SecretString"]
    creds = json.loads(raw)
    return YouTubeClient(creds["client_id"], creds["client_secret"], creds["refresh_token"])


def upload_from_s3(yt: YouTubeClient, s3, bucket: str, key: str, **meta) -> str:
    size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    return yt.upload(body, size, **meta)


def start_publishing(job: Job, yt: YouTubeClient, s3, bucket: str) -> None:
    """APPROVED -> PUBLISHING, uploading the main video unlisted when we own it."""
    if not job.existing_youtube_id and not job.main.youtube_id:
        job.main.youtube_id = upload_from_s3(
            yt, s3, bucket, job.video_key,
            title=job.main.title or job.video_key.rsplit("/", 1)[-1],
            description=job.main.description,
            tags=job.main.tags,
            privacy="unlisted",
        )
        job.main.unlisted_at = now_iso()
    job.transition(STATUS_PUBLISHING)
    storage.save_job(job)


def publish_short(job: Job, short, yt: YouTubeClient, s3, bucket: str) -> None:
    if not short.s3_key:
        raise RuntimeError(f"{short.short_id}: no rendered clip (missing s3_key)")
    short.youtube_id = upload_from_s3(
        yt, s3, bucket, short.s3_key,
        title=short.title,
        description=short_description_with_link(short, job.main_video_link()),
        tags=short.tags,
        privacy="public",
    )
    short.published_at = now_iso()
    storage.save_job(job)


def finish_if_done(job: Job, yt: YouTubeClient) -> bool:
    if job.status != STATUS_PUBLISHING or not job.all_shorts_published():
        return False
    if job.main.youtube_id and not job.main.public_at:
        yt.set_privacy(job.main.youtube_id, "public")
        job.main.public_at = now_iso()
    job.transition(STATUS_DONE)
    storage.save_job(job)
    return True


def record_error(job: Job, exc: Exception) -> None:
    job.error = f"{type(exc).__name__}: {exc}"[:2000]
    if job.status != STATUS_ERROR:
        try:
            job.transition(STATUS_ERROR)
        except ValueError:
            job.status = STATUS_ERROR
    storage.save_job(job)


def handler(event, context):
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    yt = load_client()
    summary = {"started": [], "published": None, "completed": [], "errors": []}

    jobs = storage.list_jobs()

    for job in jobs:
        if job.status != STATUS_APPROVED:
            continue
        try:
            start_publishing(job, yt, s3, bucket)
            summary["started"].append(job.job_id)
        except Exception as exc:  # noqa: BLE001 — one bad job must not block the rest
            record_error(job, exc)
            summary["errors"].append({"job_id": job.job_id, "error": str(exc)[:200]})

    pick = next_short_to_publish(jobs)
    if pick:
        job, short = pick
        try:
            publish_short(job, short, yt, s3, bucket)
            summary["published"] = {"job_id": job.job_id, "short_id": short.short_id,
                                    "youtube_id": short.youtube_id}
        except Exception as exc:  # noqa: BLE001
            record_error(job, exc)
            summary["errors"].append({"job_id": job.job_id, "error": str(exc)[:200]})

    for job in jobs:
        try:
            if finish_if_done(job, yt):
                summary["completed"].append(job.job_id)
        except Exception as exc:  # noqa: BLE001
            record_error(job, exc)
            summary["errors"].append({"job_id": job.job_id, "error": str(exc)[:200]})

    return summary
