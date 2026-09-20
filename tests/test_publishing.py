from datetime import UTC, datetime, timedelta

import pytest

from autopublisher import domain
from autopublisher.publishing import (
    FakeYouTubeAdapter,
    PublishBlocked,
    Publisher,
    RealYouTubeAdapter,
    find_duplicate_uploads,
    quota_report,
)
from autopublisher.review import Actor
from autopublisher.youtube import YouTubeAuthError, YouTubeClient, YouTubeError, YouTubeQuotaError
from tests import helpers
from tests.test_review import reviewed  # noqa: F401 - fixture reuse

OWNER = Actor("owner", "127.0.0.1")
NOW = datetime(2099, 7, 1, 8, 30, tzinfo=UTC)


@pytest.fixture
def approved(reviewed):  # noqa: F811
    svc = reviewed["service"]
    for asset in ("master", "short-01"):
        svc.update_metadata(helpers.PROJECT, "qav-test-0001", asset, {"owner_confirmed_audience": True}, OWNER)
    svc.approve_selected(helpers.PROJECT, "qav-test-0001", OWNER, include_master=True, short_ids=["short-01"])
    svc.schedule(helpers.PROJECT, "qav-test-0001", "master", "2099-07-01T10:00", OWNER)
    svc.schedule(helpers.PROJECT, "qav-test-0001", "short-01", "2099-07-02T10:00", OWNER)
    reviewed["service"] = svc
    reviewed["settings"] = helpers.settings(publish_kill_switch=False)
    return reviewed


def make_publisher(env, adapter=None, **overrides):
    settings = helpers.settings(publish_kill_switch=False, **overrides)
    return Publisher(settings, env["store"], env["jobs"], adapter or FakeYouTubeAdapter())


def test_kill_switch_blocks_everything(approved):
    publisher = Publisher(helpers.settings(), approved["store"], approved["jobs"], FakeYouTubeAdapter())
    outcomes = publisher.run_due(NOW)
    assert outcomes[0].status == "BLOCKED" and "KILL_SWITCH" in outcomes[0].message
    assert approved["load"]().state == domain.SCHEDULED


def test_due_schedule_uploads_private_once_and_records_id(approved):
    adapter = FakeYouTubeAdapter()
    publisher = make_publisher(approved, adapter)
    outcomes = publisher.run_due(NOW)
    assert [o.status for o in outcomes] == ["UPLOADED"]  # only the master is due; the short is tomorrow
    upload = adapter.requests[0]
    assert upload["privacy"] == "private" and upload["notify"] is False and upload["publish_at"] == ""
    assert upload["metadata"]["title"]
    pubs = approved["jobs"].list_publications()
    assert len(pubs) == 1 and pubs[0].youtube_video_id == outcomes[0].video_id
    assert pubs[0].verified_metadata["privacyStatus"] == "private"
    job = approved["load"]()
    assert job.state == domain.VERIFYING  # a schedule is still pending: not final yet
    # Running again must not upload again.
    again = publisher.run_due(NOW)
    assert again == [] or all(o.status != "UPLOADED" for o in again)
    assert len([r for r in adapter.requests if r["op"] == "upload"]) == 1
    # The short becomes due; final state is the honest ceiling for an unaudited project.
    later = publisher.run_due(NOW + timedelta(days=1, hours=2))
    assert [o.status for o in later] == ["UPLOADED"]
    assert approved["load"]().state == domain.UPLOADED_PRIVATE
    assert len(approved["jobs"].list_publications()) == 2
    assert find_duplicate_uploads(approved["jobs"]) == []
    actions = [e.action for e in approved["jobs"].list_audit(helpers.PROJECT, "qav-test-0001")]
    assert actions.count("UPLOADED_PRIVATE") == 2 and "FINAL" in actions


def test_audited_project_requests_publish_at_and_reaches_published(approved):
    adapter = FakeYouTubeAdapter()
    publisher = make_publisher(approved, adapter, api_project_audited=True)
    publisher.run_due(NOW)
    assert adapter.requests[0]["publish_at"] == "2099-07-01T08:00:00Z"
    assert adapter.requests[0]["privacy"] == "private"
    publisher.run_due(NOW + timedelta(days=1, hours=2))
    assert approved["load"]().state == domain.PUBLISHED


def test_retry_after_transient_error_does_not_duplicate(approved):
    adapter = FakeYouTubeAdapter(fail_with=YouTubeError("boom"), fail_times=1)
    publisher = make_publisher(approved, adapter)
    first = publisher.run_due(NOW)
    assert first[0].status == "UPLOAD_ERROR"
    sched = next(s for s in approved["jobs"].list_schedules() if s.asset_id == "master")
    assert sched.status == "UPLOAD_ERROR" and sched.attempts == 1
    assert approved["load"]().state == domain.UPLOADING_PRIVATE
    # Operator resets the schedule to PENDING (redrive); the publication row already exists.
    sched.status = "PENDING"
    approved["jobs"].update_schedule(sched, expected_version=sched.version)
    second = publisher.run_due(NOW)
    assert second[0].status == "UPLOADED"
    assert len([r for r in adapter.requests if r["op"] == "upload"]) == 2
    assert len(approved["jobs"].list_publications()) == 1


def test_auth_revocation_and_quota_are_recoverable_states(approved):
    for exc, status in ((YouTubeAuthError("invalid_grant"), "AUTH_REQUIRED"), (YouTubeQuotaError("quotaExceeded"), "QUOTA_EXHAUSTED")):
        adapter = FakeYouTubeAdapter(fail_with=exc, fail_times=5)
        publisher = make_publisher(approved, adapter)
        outcome = publisher.run_due(NOW)[0]
        assert outcome.status == status
        sched = next(s for s in approved["jobs"].list_schedules() if s.asset_id == "master")
        assert sched.status == status
        assert len([r for r in adapter.requests if r["op"] == "upload"]) == 1  # no aggressive retry
        sched.status = "PENDING"
        approved["jobs"].update_schedule(sched, expected_version=sched.version)


def test_daily_cap_is_enforced(approved):
    publisher = make_publisher(approved, daily_upload_cap=0)
    outcomes = publisher.run_due(NOW)
    assert outcomes[0].status == "BLOCKED" and "cap" in outcomes[0].message


def test_quota_report_counts_only_real_uploads(approved):
    publisher = make_publisher(approved)
    publisher.run_due(NOW)
    report = quota_report(approved["jobs"], helpers.settings(), datetime.now(UTC))
    assert report.uploads_today == 0 and report.allowed


def test_unapproved_asset_is_refused_even_if_scheduled(approved):
    sched = next(s for s in approved["jobs"].list_schedules() if s.asset_id == "master")
    rev = approved["jobs"].get_revision(helpers.PROJECT, "qav-test-0001", 1)
    rev.master_review_status = domain.PENDING
    approved["jobs"].update_revision_review(rev)
    outcome = make_publisher(approved).publish_schedule(sched, NOW)
    assert outcome.status == "FAILED" and "not approved" in outcome.message
    assert approved["load"]().state == domain.SCHEDULED


def test_real_adapter_requires_real_mode(approved):
    adapter = RealYouTubeAdapter(YouTubeClient("id", "secret", "token"))
    with pytest.raises(PublishBlocked):
        Publisher(helpers.settings(publish_kill_switch=False), approved["store"], approved["jobs"], adapter)


def test_no_automatic_public_flip(approved):
    adapter = FakeYouTubeAdapter()
    publisher = make_publisher(approved, adapter)
    publisher.run_due(NOW + timedelta(days=2))
    assert all(v["privacyStatus"] == "private" for v in adapter.videos.values())
    assert not [r for r in adapter.requests if r["op"] not in ("upload", "status", "thumbnail")]
