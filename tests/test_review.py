import pytest

from autopublisher import domain, ingest
from autopublisher.ingest import Ingestor, ReadyEvent
from autopublisher.jobstore import ConflictError, LocalJobStore
from autopublisher.objectstore import FilesystemObjectStore
from autopublisher.processing import Processor
from autopublisher.providers.analysis import ManifestAnalysisProvider
from autopublisher.providers.transcription import ManifestTranscriptionProvider
from autopublisher.review import Actor, ReviewError, ReviewService
from tests import helpers

OWNER = Actor("owner", "127.0.0.1")


@pytest.fixture
def reviewed(tmp_path):
    bucket = tmp_path / "bucket"
    helpers.make_job_dir(bucket)
    store = FilesystemObjectStore(bucket)
    jobs = LocalJobStore(tmp_path / "state")
    settings = helpers.settings()
    result = Ingestor(settings, store, jobs, workdir=tmp_path).handle_ready(ReadyEvent("b", helpers.ready_key()))
    assert result.status == ingest.CREATED
    processor = Processor(settings, store, jobs, ManifestTranscriptionProvider(), ManifestAnalysisProvider(), workdir=tmp_path)
    processor.run(result.job)
    return {"jobs": jobs, "store": store, "service": ReviewService(settings, jobs), "processor": processor,
            "load": lambda: jobs.get_job(helpers.PROJECT, "qav-test-0001")}


def confirm(service, asset_id="master"):
    service.update_metadata(helpers.PROJECT, "qav-test-0001", asset_id, {"owner_confirmed_audience": True}, OWNER)


def test_approval_requires_audience_confirmation(reviewed):
    svc = reviewed["service"]
    with pytest.raises(ReviewError, match="made-for-kids"):
        svc.approve_master(helpers.PROJECT, "qav-test-0001", OWNER)
    confirm(svc)
    revision = svc.approve_master(helpers.PROJECT, "qav-test-0001", OWNER)
    assert revision.master_review_status == domain.REVIEW_APPROVED
    assert reviewed["load"]().state == domain.AWAITING_REVIEW  # approving the master alone does not move the job


def test_metadata_edit_is_validated_and_audited(reviewed):
    svc = reviewed["service"]
    rev = svc.update_metadata(helpers.PROJECT, "qav-test-0001", "master",
                              {"title": "Quiz sui fiumi italiani", "hashtags": ["#quiz", "#geografia"]}, OWNER)
    assert rev.master_metadata["title"] == "Quiz sui fiumi italiani"
    with pytest.raises(ReviewError):
        svc.update_metadata(helpers.PROJECT, "qav-test-0001", "master", {"title": "x" * 101}, OWNER)
    with pytest.raises(ReviewError):
        svc.update_metadata(helpers.PROJECT, "qav-test-0001", "master", {"hashtags": ["a", "b", "c", "d"]}, OWNER)
    with pytest.raises(ReviewError):
        svc.update_metadata(helpers.PROJECT, "qav-test-0001", "master", {"privacy": "public"}, OWNER)
    stored = reviewed["jobs"].get_revision(helpers.PROJECT, "qav-test-0001", 1)
    assert stored.master_metadata["title"] == "Quiz sui fiumi italiani"
    actions = [e.action for e in reviewed["jobs"].list_audit(helpers.PROJECT, "qav-test-0001")]
    assert actions.count("METADATA_EDITED") == 1
    assert reviewed["jobs"].list_audit(helpers.PROJECT, "qav-test-0001")[-1].source_ip == "127.0.0.1"


def test_short_reject_needs_reason_and_approve_needs_quality(reviewed):
    svc = reviewed["service"]
    with pytest.raises(ReviewError):
        svc.review_short(helpers.PROJECT, "qav-test-0001", "short-01", False, OWNER, reason="  ")
    rev = svc.review_short(helpers.PROJECT, "qav-test-0001", "short-01", False, OWNER, reason="caption overlaps")
    assert rev.shorts[0].review_status == domain.REVIEW_REJECTED
    confirm(svc, "short-01")
    rev = svc.review_short(helpers.PROJECT, "qav-test-0001", "short-01", True, OWNER)
    assert rev.shorts[0].review_status == domain.REVIEW_APPROVED
    rev.shorts[0].quality["ok"] = False
    reviewed["jobs"].update_revision_review(rev)  # review fields only: quality stays as rendered
    assert reviewed["jobs"].get_revision(helpers.PROJECT, "qav-test-0001", 1).shorts[0].quality["ok"] is True


def test_batch_approval_moves_job_and_leaves_unselected_pending(reviewed):
    svc = reviewed["service"]
    with pytest.raises(ReviewError):
        svc.approve_selected(helpers.PROJECT, "qav-test-0001", OWNER, include_master=False, short_ids=[])
    confirm(svc)
    job = svc.approve_selected(helpers.PROJECT, "qav-test-0001", OWNER, include_master=True, short_ids=[])
    assert job.state == domain.APPROVED
    rev = reviewed["jobs"].get_revision(helpers.PROJECT, "qav-test-0001", 1)
    assert rev.master_review_status == domain.REVIEW_APPROVED
    assert rev.shorts[0].review_status == domain.PENDING
    with pytest.raises(ReviewError):
        svc.approve_master(helpers.PROJECT, "qav-test-0001", OWNER)


def test_reject_with_scope_then_reprocess_then_approve_revised(reviewed):
    svc, proc = reviewed["service"], reviewed["processor"]
    with pytest.raises(ReviewError):
        svc.reject(helpers.PROJECT, "qav-test-0001", "", domain.SHORTS_ONLY, OWNER)
    with pytest.raises(ReviewError):
        svc.reject(helpers.PROJECT, "qav-test-0001", "bad crop", "EVERYTHING", OWNER)
    job = svc.reject(helpers.PROJECT, "qav-test-0001", "crop cuts the answer", domain.SHORTS_ONLY, OWNER)
    assert job.state == domain.REPROCESS_REQUESTED and job.reprocess_scope == domain.SHORTS_ONLY
    rev2 = proc.run(job, scope=job.reprocess_scope, reason="crop cuts the answer")
    assert rev2.number == 2 and reviewed["load"]().state == domain.AWAITING_REVIEW
    assert reviewed["jobs"].get_revision(helpers.PROJECT, "qav-test-0001", 1).reject_reason == "crop cuts the answer"
    confirm(svc)
    confirm(svc, "short-01")
    job = svc.approve_selected(helpers.PROJECT, "qav-test-0001", OWNER, include_master=True, short_ids=["short-01"])
    assert job.state == domain.APPROVED and job.current_revision == 2
    read = svc.read_model(helpers.PROJECT, "qav-test-0001")
    assert read["state"] == domain.APPROVED and read["shorts"][0]["review_status"] == domain.REVIEW_APPROVED
    assert set(read) == {"project_key", "job_id", "state", "current_revision", "master_review_status", "shorts", "updated_at"}


def test_concurrent_review_conflict(reviewed):
    svc = reviewed["service"]
    stale = reviewed["load"]()
    svc.update_metadata(helpers.PROJECT, "qav-test-0001", "master", {"title": "Nuovo titolo"}, OWNER)
    with pytest.raises(ConflictError):
        svc.update_metadata(helpers.PROJECT, "qav-test-0001", "master", {"title": "Altro"}, Actor("other", "10.0.0.2"),
                            expected_version=stale.version)


def test_schedule_requires_approval_and_future_time(reviewed):
    svc = reviewed["service"]
    with pytest.raises(ReviewError):
        svc.schedule(helpers.PROJECT, "qav-test-0001", "master", "2099-01-01T10:00", OWNER)
    confirm(svc)
    svc.approve_selected(helpers.PROJECT, "qav-test-0001", OWNER, include_master=True, short_ids=[])
    with pytest.raises(ReviewError):
        svc.schedule(helpers.PROJECT, "qav-test-0001", "short-01", "2099-01-01T10:00", OWNER)
    with pytest.raises(ReviewError):
        svc.schedule(helpers.PROJECT, "qav-test-0001", "master", "2000-01-01T10:00", OWNER)
    sched = svc.schedule(helpers.PROJECT, "qav-test-0001", "master", "2099-07-01T10:00", OWNER)
    assert sched.publish_at_utc == "2099-07-01T08:00:00Z"  # Europe/Rome summer time -> UTC
    assert sched.owner_timezone == "Europe/Rome"
    assert reviewed["load"]().state == domain.SCHEDULED
    again = svc.schedule(helpers.PROJECT, "qav-test-0001", "master", "2099-07-01T12:00", OWNER)
    assert again.schedule_id == sched.schedule_id and again.publish_at_utc == "2099-07-01T10:00:00Z"
    job = svc.cancel(helpers.PROJECT, "qav-test-0001", "changed my mind", OWNER)
    assert job.state == domain.CANCELLED
    assert reviewed["jobs"].list_schedules()[0].status == "CANCELLED"


def test_cancel_only_unpublished(reviewed):
    svc = reviewed["service"]
    job = reviewed["load"]()
    job.state = domain.PUBLISHED
    reviewed["jobs"].update_job(job, expected_version=job.version)
    with pytest.raises(ReviewError):
        svc.cancel(helpers.PROJECT, "qav-test-0001", "x", OWNER)
