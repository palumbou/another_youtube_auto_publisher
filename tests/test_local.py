"""CLI-level tests of the local runner: every subcommand must persist its effect."""

import json

import pytest

from autopublisher import domain
from autopublisher.jobstore import LocalJobStore
from autopublisher.local import main
from tests import helpers


@pytest.fixture
def stack_dirs(tmp_path):
    bucket, state = tmp_path / "bucket", tmp_path / "state"
    helpers.make_job_dir(bucket)
    assert main(["ingest", "--bucket-root", str(bucket), "--state-root", str(state)]) == 0
    jobs = LocalJobStore(state)
    assert jobs.get_job(helpers.PROJECT, "qav-test-0001").state == domain.AWAITING_REVIEW
    return bucket, state, jobs


def test_confirm_persists_the_flag_for_a_short_and_audits_it(stack_dirs, capsys):
    _, state, jobs = stack_dirs
    assert main(["confirm", "--state-root", str(state), "--job", "qav-test-0001", "--asset", "short-01"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["asset_id"] == "short-01" and printed["owner_confirmed_audience"] is True
    revision = jobs.get_revision(helpers.PROJECT, "qav-test-0001", 1)
    assert revision.shorts[0].metadata["owner_confirmed_audience"] is True
    assert "owner_confirmed_audience" not in revision.master_metadata or not revision.master_metadata["owner_confirmed_audience"]
    edits = [e for e in jobs.list_audit(helpers.PROJECT, "qav-test-0001") if e.action == "METADATA_EDITED"]
    assert [e.details["asset_id"] for e in edits] == ["short-01"]
    assert edits[0].details["changed"] == ["owner_confirmed_audience"]
    # The confirmation is what unlocks approval of that short.
    assert main(["confirm", "--state-root", str(state), "--job", "qav-test-0001", "--asset", "master", "--synthetic"]) == 0
    assert json.loads(capsys.readouterr().out)["owner_confirmed_synthetic"] is True
    assert main(["approve", "--state-root", str(state), "--job", "qav-test-0001", "--master", "--short", "short-01"]) == 0
    assert json.loads(capsys.readouterr().out) == domain.APPROVED
    revision = jobs.get_revision(helpers.PROJECT, "qav-test-0001", 1)
    assert revision.shorts[0].review_status == domain.REVIEW_APPROVED


def test_approve_without_confirmation_fails_loudly(stack_dirs):
    _, state, jobs = stack_dirs
    with pytest.raises(Exception, match="made-for-kids"):
        main(["approve", "--state-root", str(state), "--job", "qav-test-0001", "--master", "--short", "short-01"])
    assert jobs.get_job(helpers.PROJECT, "qav-test-0001").state == domain.AWAITING_REVIEW


def test_status_reject_schedule_publish_round_trip(stack_dirs, capsys):
    bucket, state, jobs = stack_dirs
    b, s = str(bucket), str(state)
    assert main(["status", "--state-root", s]) == 0
    assert json.loads(capsys.readouterr().out)[0]["state"] == domain.AWAITING_REVIEW
    assert main(["reject", "--bucket-root", b, "--state-root", s, "--job", "qav-test-0001", "--reason", "crop", "--scope", "SHORTS_ONLY"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["revision"] == 2
    for asset in ("master", "short-01"):
        assert main(["confirm", "--state-root", s, "--job", "qav-test-0001", "--asset", asset]) == 0
        capsys.readouterr()
    assert main(["approve", "--state-root", s, "--job", "qav-test-0001", "--master", "--short", "short-01"]) == 0
    capsys.readouterr()
    assert main(["schedule", "--state-root", s, "--job", "qav-test-0001", "--asset", "short-01", "--at", "2099-01-01T10:00"]) == 0
    assert json.loads(capsys.readouterr().out)["publish_at_utc"] == "2099-01-01T09:00:00Z"
    assert main(["publish", "--bucket-root", b, "--state-root", s, "--now", "2099-01-01T10:00:00Z"]) == 0
    outcomes = json.loads(capsys.readouterr().out)
    assert [o["status"] for o in outcomes] == ["UPLOADED"]
    assert jobs.list_publications()[0].youtube_video_id.startswith("fake-")
    assert main(["status", "--state-root", s, "--job", "qav-test-0001"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == domain.UPLOADED_PRIVATE
    assert "SHORT_APPROVED" not in [a["action"] for a in status["audit"]]  # batch approval path used
    assert "BATCH_APPROVED" in [a["action"] for a in status["audit"]]
