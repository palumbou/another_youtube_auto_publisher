import json

import pytest

from autopublisher import storage, webui
from autopublisher.models import (
    STATUS_ANALYZING,
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING_REVIEW,
    Job,
    Short,
)

TOKEN = "test-token-123"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("WEBUI_TOKEN", TOKEN)
    monkeypatch.setenv("BUCKET", "test-bucket")
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:eu-west-1:123:stateMachine:x")


@pytest.fixture
def db(monkeypatch):
    """In-memory replacement for the DynamoDB-backed storage module."""
    jobs: dict[str, Job] = {}
    monkeypatch.setattr(storage, "save_job", lambda job: jobs.__setitem__(job.job_id, job))
    monkeypatch.setattr(storage, "load_job", jobs.get)
    monkeypatch.setattr(storage, "list_jobs", lambda: sorted(
        jobs.values(), key=lambda j: j.created_at, reverse=True))
    return jobs


@pytest.fixture
def aws_stubs(monkeypatch):
    calls = {"presigned": [], "executions": []}
    monkeypatch.setattr(webui, "presign", lambda key: (
        calls["presigned"].append(key) or f"https://signed/{key}"))
    monkeypatch.setattr(webui, "start_reanalysis", lambda job: (
        calls["executions"].append(job.job_id)))
    return calls


def request(path="/", method="GET", token_cookie=TOKEN, query=None, body=""):
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "cookies": [f"{webui.COOKIE}={token_cookie}"] if token_cookie else [],
        "body": body,
    }
    if query:
        event["queryStringParameters"] = query
    return event


def make_job(job_id="my-video-abc12345", status=STATUS_PENDING_REVIEW):
    job = Job(job_id=job_id, video_key=f"incoming/{job_id}.mp4", status=status,
              created_at="2026-07-01T00:00:00Z")
    job.shorts = [Short("short-01", 10, 40, title="A Short", s3_key=f"ready/{job_id}/short-01.mp4")]
    return job


class TestAuth:
    def test_denied_without_token(self, db):
        assert webui.handler(request(token_cookie=None), None)["statusCode"] == 403

    def test_denied_with_wrong_token(self, db):
        assert webui.handler(request(token_cookie="nope"), None)["statusCode"] == 403

    def test_query_token_sets_cookie_and_redirects(self, db):
        response = webui.handler(request(token_cookie=None, query={"token": TOKEN}), None)
        assert response["statusCode"] == 303
        assert response["headers"]["Location"] == "/"
        assert any(TOKEN in c for c in response["cookies"])


class TestPages:
    def test_index_lists_jobs(self, db, aws_stubs):
        db["j1"] = make_job("j1")
        response = webui.handler(request("/"), None)
        assert response["statusCode"] == 200
        assert "j1" in response["body"]
        assert "PENDING_REVIEW" in response["body"]

    def test_job_detail_presigns_clips(self, db, aws_stubs):
        job = make_job()
        db[job.job_id] = job
        response = webui.handler(request(f"/job/{job.job_id}"), None)
        assert response["statusCode"] == 200
        assert "A Short" in response["body"]
        assert aws_stubs["presigned"] == [job.shorts[0].s3_key]

    def test_unknown_job_404(self, db):
        assert webui.handler(request("/job/nope"), None)["statusCode"] == 404

    def test_html_is_escaped(self, db, aws_stubs):
        job = make_job()
        job.main.title = "<script>alert(1)</script>"
        db[job.job_id] = job
        body = webui.handler(request(f"/job/{job.job_id}"), None)["body"]
        assert "<script>alert(1)" not in body


class TestApprove:
    def test_approve_transitions(self, db, aws_stubs):
        job = make_job()
        db[job.job_id] = job
        response = webui.handler(request(f"/job/{job.job_id}/approve", "POST"), None)
        assert response["statusCode"] == 303
        assert db[job.job_id].status == STATUS_APPROVED

    def test_approve_from_wrong_status_conflicts(self, db, aws_stubs):
        job = make_job(status=STATUS_DONE)
        db[job.job_id] = job
        response = webui.handler(request(f"/job/{job.job_id}/approve", "POST"), None)
        assert response["statusCode"] == 409
        assert db[job.job_id].status == STATUS_DONE


class TestReanalyze:
    def test_reanalyze_restarts_pipeline_with_prompt(self, db, aws_stubs):
        job = make_job(status=STATUS_ERROR)
        job.error = "boom"
        db[job.job_id] = job
        response = webui.handler(
            request(f"/job/{job.job_id}/reanalyze", "POST", body="prompt=focus+on+cats"), None)
        assert response["statusCode"] == 303
        saved = db[job.job_id]
        assert saved.status == STATUS_ANALYZING
        assert saved.prompt == "focus on cats"
        assert saved.error == ""
        assert aws_stubs["executions"] == [job.job_id]

    def test_reanalyze_from_done_conflicts(self, db, aws_stubs):
        job = make_job(status=STATUS_DONE)
        db[job.job_id] = job
        response = webui.handler(request(f"/job/{job.job_id}/reanalyze", "POST"), None)
        assert response["statusCode"] == 409
        assert aws_stubs["executions"] == []


class TestReanalysisInput:
    def test_start_reanalysis_payload(self, db, monkeypatch):
        sent = {}

        class FakeSfn:
            def start_execution(self, **kwargs):
                sent.update(kwargs)

        import boto3
        monkeypatch.setattr(boto3, "client", lambda name: FakeSfn())
        job = make_job()
        job.prompt = "guidance"
        webui.start_reanalysis(job)
        payload = json.loads(sent["input"])
        assert payload["reanalyze"] is True
        assert payload["job_id"] == job.job_id
        assert payload["summary"]["transcript_key"] == f"work/{job.job_id}/transcript.json"
