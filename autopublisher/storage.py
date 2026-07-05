"""DynamoDB persistence for jobs. Kept thin so the domain model stays AWS-free."""

from __future__ import annotations

import json
import os
from decimal import Decimal

import boto3

from autopublisher.models import Job

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
    return _table


def _to_dynamo(value):
    """DynamoDB refuses floats: round-trip through JSON parsing floats as Decimal."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def _from_dynamo(value):
    return json.loads(json.dumps(value, default=lambda d: float(d) if isinstance(d, Decimal) else str(d)))


def save_job(job: Job) -> None:
    table().put_item(Item=_to_dynamo(job.to_item()))


def load_job(job_id: str) -> Job | None:
    response = table().get_item(Key={"job_id": job_id})
    item = response.get("Item")
    return Job.from_item(_from_dynamo(item)) if item else None


def list_jobs() -> list[Job]:
    items = []
    kwargs = {}
    while True:
        response = table().scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    jobs = [Job.from_item(_from_dynamo(i)) for i in items]
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)
