import json
from datetime import UTC, datetime

import pytest

from autopublisher import contract, domain, ingest
from autopublisher.ingest import Ingestor, ReadyEvent
from autopublisher.jobstore import LocalJobStore
from autopublisher.objectstore import FilesystemObjectStore
from tests import helpers

FIXTURES = contract.CONTRACT_DIR / "1.0.0" / "fixtures" / "events"


@pytest.fixture
def bucket(tmp_path):
    return tmp_path / "bucket"


@pytest.fixture
def ingestor(bucket, tmp_path):
    def build(**overrides):
        return Ingestor(helpers.settings(**overrides), FilesystemObjectStore(bucket),
                        LocalJobStore(tmp_path / "state"), workdir=tmp_path)
    return build


def event(job_id="qav-test-0001"):
    return ReadyEvent(bucket="local", key=helpers.ready_key(job_id))


def test_valid_job_is_created_once(bucket, ingestor):
    helpers.make_job_dir(bucket)
    ing = ingestor()
    result = ing.handle_ready(event())
    assert result.status == ingest.CREATED, result.as_dict()
    job = result.job
    assert job.state == domain.INGESTED
    assert job.source_duration_ms == 2000 and job.source_width == 320
    assert job.source_sha256 == json.loads(
        (bucket / "incoming/quiz-al-volo/qav-test-0001/manifest/video-job-manifest.json").read_text()
    )["source"]["sha256"]
    events = ing.jobs.list_audit(helpers.PROJECT, "qav-test-0001")
    assert [e.action for e in events] == ["INGEST_ACCEPTED"]
    assert "cutover" in events[0].details


def test_duplicate_and_out_of_order_events_create_one_job(bucket, ingestor):
    helpers.make_job_dir(bucket)
    ing = ingestor()
    fixture = json.loads((FIXTURES / "duplicate-and-out-of-order-ready-events.json").read_text())
    results = [ing.handle_ready(ReadyEvent.from_dict(e | {"key": helpers.ready_key()})) for e in fixture["events"]]
    assert [r.status for r in results] == [ingest.CREATED, ingest.DUPLICATE, ingest.DUPLICATE]
    assert len(ing.jobs.list_jobs()) == 1
    assert all(r.job.idempotency_key == results[0].job.idempotency_key for r in results)


def test_pre_cutover_ready_is_ignored(bucket, ingestor):
    helpers.make_job_dir(bucket)
    ing = ingestor(cutover_at=datetime(2099, 1, 1, tzinfo=UTC))
    result = ing.handle_ready(event())
    assert (result.status, result.code) == (ingest.IGNORED, contract.PRE_CUTOVER_JOB)
    assert ing.jobs.list_jobs() == []
    assert ing.jobs.list_audit(helpers.PROJECT, "qav-test-0001")[0].reason == contract.PRE_CUTOVER_JOB


def test_missing_ready_is_ignored(bucket, ingestor):
    helpers.make_job_dir(bucket, with_ready=False)
    ing = ingestor()
    fixture = json.loads((FIXTURES / "missing-ready.json").read_text())
    manifest_event = ReadyEvent.from_dict(fixture["event"] | {
        "key": "incoming/quiz-al-volo/qav-test-0001/manifest/video-job-manifest.json"})
    assert ing.handle_ready(manifest_event).status == ingest.IGNORED
    # A READY event whose object never appeared is also ignored, with the contract code.
    result = ing.handle_ready(event())
    assert (result.status, result.code) == (ingest.IGNORED, contract.MISSING_READY_DEPENDENCY)
    assert ing.jobs.list_jobs() == []


def test_unsupported_project_is_rejected(bucket, ingestor):
    helpers.make_job_dir(bucket, project="other")
    result = ingestor().handle_ready(ReadyEvent(bucket="b", key=helpers.ready_key(project="other")))
    assert (result.status, result.code) == (ingest.REJECTED, contract.UNSUPPORTED_PROJECT)


@pytest.mark.parametrize("key", ["incoming/quiz-al-volo/../x/READY", "incoming/quiz-al-volo/UPPER/READY",
                                 "/incoming/quiz-al-volo/a/READY", "other/quiz-al-volo/a/READY"])
def test_unsafe_or_foreign_keys_never_create_jobs(bucket, ingestor, key):
    result = ingestor().handle_ready(ReadyEvent(bucket="b", key=key))
    assert result.status == ingest.IGNORED


def test_hash_mismatch_is_rejected(bucket, ingestor):
    helpers.make_job_dir(bucket, manifest=helpers.tiny_manifest(sha256="f" * 64))
    result = ingestor().handle_ready(event())
    assert (result.status, result.code) == (ingest.REJECTED, contract.SOURCE_HASH_MISMATCH)
    assert result.errors[0].pointer == "/source/sha256"


def test_schema_and_semantic_failures_are_rejected_with_codes(bucket, ingestor):
    bad = helpers.tiny_manifest()
    bad["schema_version"] = "2.0.0"
    helpers.make_job_dir(bucket, manifest=bad)
    result = ingestor().handle_ready(event())
    assert (result.status, result.code) == (ingest.REJECTED, contract.UNSUPPORTED_SCHEMA_VERSION)
    bad = helpers.tiny_manifest()
    bad["segments"][3], bad["segments"][2] = bad["segments"][2], bad["segments"][3]
    helpers.make_job_dir(bucket, job_id="qav-test-0002", manifest=bad | {"job_id": "qav-test-0002"})
    result = ingestor().handle_ready(event("qav-test-0002"))
    assert (result.status, result.code) == (ingest.REJECTED, contract.SEGMENT_ORDER_INVALID)


def test_manifest_required_for_quiz_al_volo(bucket, ingestor):
    helpers.make_job_dir(bucket, manifest=False)
    result = ingestor().handle_ready(event())
    assert (result.status, result.code) == (ingest.REJECTED, contract.MISSING_READY_DEPENDENCY)


def test_media_mismatch_and_corrupt_media(bucket, ingestor):
    wrong = helpers.tiny_manifest()
    wrong["source"]["width"] = 1920
    wrong["source"]["height"] = 1080
    helpers.make_job_dir(bucket, manifest=wrong)
    result = ingestor().handle_ready(event())
    assert (result.status, result.code) == (ingest.REJECTED, contract.INVALID_MEDIA)
    corrupt = b"not a video at all" * 100
    import hashlib
    manifest = helpers.tiny_manifest(job_id="qav-test-0003", sha256=hashlib.sha256(corrupt).hexdigest())
    prefix = helpers.make_job_dir(bucket, job_id="qav-test-0003", manifest=manifest)
    (prefix / "source" / "master.mp4").write_bytes(corrupt)
    result = ingestor().handle_ready(event("qav-test-0003"))
    assert (result.status, result.code) == (ingest.REJECTED, contract.INVALID_MEDIA)


def test_unexpected_objects_are_rejected(bucket, ingestor):
    helpers.make_job_dir(bucket, extra={"assets/payload.exe": b"MZ", "notes.txt": b"x"})
    result = ingestor().handle_ready(event())
    assert result.status == ingest.REJECTED
    assert {e.code for e in result.errors} >= {contract.INVALID_MEDIA, contract.UNSAFE_OBJECT_KEY}


def test_changed_source_under_same_job_id_is_rejected(bucket, ingestor):
    helpers.make_job_dir(bucket)
    ing = ingestor()
    assert ing.handle_ready(event()).status == ingest.CREATED
    # The producer silently rewrites the source: the version id and the hash change.
    prefix = bucket / "incoming/quiz-al-volo/qav-test-0001"
    (prefix / "source" / "master.mp4").write_bytes(helpers.TINY.read_bytes() + b"\x00")
    result = ing.handle_ready(event())
    assert (result.status, result.code) == (ingest.REJECTED, contract.SOURCE_VERSION_CHANGED)
    assert len(ing.jobs.list_jobs()) == 1
    assert ing.jobs.list_jobs()[0].state == domain.INGESTED


def test_oversized_source_is_rejected(bucket, ingestor):
    helpers.make_job_dir(bucket)
    result = ingestor(max_source_bytes=1000).handle_ready(event())
    assert (result.status, result.code) == (ingest.REJECTED, contract.INVALID_MEDIA)


def test_eventbridge_and_s3_record_parsing():
    eb = {"time": "2026-09-20T10:00:00Z", "detail": {"bucket": {"name": "b"},
          "object": {"key": "incoming/p/j/READY", "version-id": "v9"}, "reason": "PutObject"}}
    parsed = ReadyEvent.from_eventbridge(eb)
    assert parsed.version_id == "v9" and parsed.event_time.tzinfo is not None
    rec = {"eventTime": "2026-09-20T10:00:00.000Z", "eventName": "ObjectCreated:Put",
           "s3": {"bucket": {"name": "b"}, "object": {"key": "incoming/p/j/READY", "versionId": "v1"}}}
    assert ReadyEvent.from_s3_record(rec).key == "incoming/p/j/READY"


def test_rejections_never_leak_manifest_free_text(bucket, ingestor):
    bad = helpers.tiny_manifest()
    bad["rights"]["notes"] = "token=DO-NOT-LEAK"
    bad["segments"][-1]["end_ms"] = 999999
    helpers.make_job_dir(bucket, manifest=bad)
    result = ingestor().handle_ready(event())
    assert result.status == ingest.REJECTED
    assert "DO-NOT-LEAK" not in json.dumps(result.as_dict())


def test_ingest_without_probe_records_declared_media(bucket, ingestor, tmp_path):
    helpers.make_job_dir(bucket)
    ing = ingestor()
    ing.probe_media = False
    result = ing.handle_ready(event())
    assert result.status == ingest.CREATED
    assert (result.job.source_width, result.job.source_duration_ms) == (320, 2000)
