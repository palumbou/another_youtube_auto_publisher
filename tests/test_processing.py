import json

import pytest

from autopublisher import domain, ingest
from autopublisher.ingest import Ingestor, ReadyEvent
from autopublisher.jobstore import LocalJobStore
from autopublisher.objectstore import FilesystemObjectStore
from autopublisher.processing import ProcessingFailed, Processor
from autopublisher.providers.analysis import (
    FailingAnalysisProvider,
    ManifestAnalysisProvider,
    UngroundedAnalysisProvider,
)
from autopublisher.providers.transcription import (
    FailingTranscriptionProvider,
    ManifestTranscriptionProvider,
)
from tests import helpers


@pytest.fixture
def env(tmp_path):
    bucket = tmp_path / "bucket"
    helpers.make_job_dir(bucket)
    store = FilesystemObjectStore(bucket)
    jobs = LocalJobStore(tmp_path / "state")
    settings = helpers.settings()
    result = Ingestor(settings, store, jobs, workdir=tmp_path).handle_ready(ReadyEvent("b", helpers.ready_key()))
    assert result.status == ingest.CREATED

    def processor(transcription=None, analysis=None):
        return Processor(settings, store, jobs, transcription or ManifestTranscriptionProvider(),
                         analysis or ManifestAnalysisProvider(), workdir=tmp_path)
    return {"store": store, "jobs": jobs, "job": result.job, "processor": processor, "bucket": bucket}


def test_full_run_reaches_awaiting_review_with_assets(env):
    revision = env["processor"]().run(env["job"])
    job = env["jobs"].get_job(helpers.PROJECT, "qav-test-0001")
    assert job.state == domain.AWAITING_REVIEW and job.current_revision == 1
    assert revision.number == 1 and revision.scope == domain.FULL
    assert [a.asset_id for a in revision.shorts] == ["short-01"]
    short = revision.shorts[0]
    assert (short.width, short.height, short.codec) == (1080, 1920, "h264")
    assert short.quality["ok"], short.quality
    assert env["store"].head(short.key).size == short.size_bytes
    assert revision.master.sha256 == job.source_sha256
    assert revision.master_metadata["title"] and len(revision.master_metadata["title_candidates"]) == 3
    assert revision.master_metadata["privacy"] == "private"
    assert short.metadata["title"] and short.metadata["description"]
    assert "Il Po" not in short.metadata["description"]  # no spoiler in the Short description either
    assert short.quality["layout"]["scale"] > 0 and isinstance(short.quality["captions"], list)
    assert revision.thumbnails and env["store"].head(revision.thumbnails[0].key)
    states = [e.new_state for e in env["jobs"].list_audit(helpers.PROJECT, "qav-test-0001") if e.action == "STATE"]
    assert states == [domain.VALIDATING, domain.ANALYZING, domain.GENERATING_ASSETS, domain.AWAITING_REVIEW]
    log = json.loads(env["store"].get_bytes(revision.command_log_key))
    assert all(entry["ok"] for entry in log)
    assert revision.checksums["short-01"] == short.sha256


def test_transcript_failure_fails_the_job_without_retry(env):
    with pytest.raises(ProcessingFailed) as info:
        env["processor"](transcription=FailingTranscriptionProvider()).run(env["job"])
    job = env["jobs"].get_job(helpers.PROJECT, "qav-test-0001")
    assert job.state == domain.FAILED and job.error["code"] == "PROVIDERERROR"
    assert info.value.code == "PROVIDERERROR"
    assert env["jobs"].list_revisions(helpers.PROJECT, "qav-test-0001") == []


def test_hallucinated_analysis_fails_the_job(env):
    with pytest.raises(ProcessingFailed):
        env["processor"](analysis=UngroundedAnalysisProvider()).run(env["job"])
    assert env["jobs"].get_job(helpers.PROJECT, "qav-test-0001").error["code"] == "GROUNDINGERROR"


def test_analysis_failure(env):
    with pytest.raises(ProcessingFailed):
        env["processor"](analysis=FailingAnalysisProvider()).run(env["job"])


def test_low_confidence_is_surfaced_not_corrected(env):
    provider = ManifestTranscriptionProvider(low_confidence_segments=("answer-01",))
    revision = env["processor"](transcription=provider).run(env["job"])
    codes = {w["code"] for w in revision.warnings}
    assert "LOW_CONFIDENCE_TRANSCRIPT" in codes
    transcript = json.loads(env["store"].get_bytes(revision.transcript_key))
    assert [s["text"] for s in transcript["segments"] if s["confidence"] < 0.75] == ["Il Po"]


def test_scoped_reprocess_creates_immutable_next_revision(env):
    proc = env["processor"]()
    first = proc.run(env["job"])
    job = env["jobs"].get_job(helpers.PROJECT, "qav-test-0001")
    job.transition(domain.REPROCESS_REQUESTED)
    env["jobs"].update_job(job, expected_version=job.version)
    second = proc.run(job, scope=domain.METADATA_ONLY, reason="title too vague")
    assert second.number == 2 and second.parent_number == 1
    assert second.transcript_key == first.transcript_key
    assert [a.key for a in second.shorts] == [a.key for a in first.shorts]
    assert second.shorts[0].review_status == domain.PENDING
    assert env["jobs"].get_revision(helpers.PROJECT, "qav-test-0001", 1).to_dict() == first.to_dict()
    job = env["jobs"].get_job(helpers.PROJECT, "qav-test-0001")
    job.transition(domain.REPROCESS_REQUESTED)
    env["jobs"].update_job(job, expected_version=job.version)
    third = proc.run(job, scope=domain.SHORTS_ONLY, reason="crop")
    assert third.number == 3 and third.analysis == first.analysis
    assert third.shorts[0].key != first.shorts[0].key
    assert env["jobs"].get_job(helpers.PROJECT, "qav-test-0001").current_revision == 3


def test_processing_refuses_wrong_state(env):
    job = env["job"]
    job.transition(domain.VALIDATING)
    env["jobs"].update_job(job, expected_version=job.version)
    with pytest.raises(ProcessingFailed):
        env["processor"]().run(env["jobs"].get_job(helpers.PROJECT, "qav-test-0001"))
