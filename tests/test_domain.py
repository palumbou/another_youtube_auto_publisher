import itertools

import pytest

from autopublisher import domain
from autopublisher.domain import InvalidTransition, Job


def make_job(state=domain.INGESTED) -> Job:
    return Job(project_key="quiz-al-volo", job_id="qav-1", environment="local", idempotency_key="k",
               source_key="incoming/quiz-al-volo/qav-1/source/master.mp4", source_sha256="a" * 64,
               source_version_id="v1", ready_version_id="r1", ready_created_at="2026-09-20T10:00:00Z",
               state=state)


EXPECTED_VALID = {
    (domain.INGESTED, domain.VALIDATING),
    (domain.VALIDATING, domain.ANALYZING), (domain.VALIDATING, domain.FAILED),
    (domain.ANALYZING, domain.GENERATING_ASSETS), (domain.ANALYZING, domain.FAILED),
    (domain.GENERATING_ASSETS, domain.AWAITING_REVIEW), (domain.GENERATING_ASSETS, domain.FAILED),
    (domain.AWAITING_REVIEW, domain.REPROCESS_REQUESTED), (domain.AWAITING_REVIEW, domain.APPROVED),
    (domain.AWAITING_REVIEW, domain.CANCELLED),
    (domain.REPROCESS_REQUESTED, domain.ANALYZING), (domain.REPROCESS_REQUESTED, domain.GENERATING_ASSETS),
    (domain.APPROVED, domain.SCHEDULED), (domain.APPROVED, domain.CANCELLED),
    (domain.SCHEDULED, domain.UPLOADING_PRIVATE), (domain.SCHEDULED, domain.CANCELLED),
    (domain.UPLOADING_PRIVATE, domain.VERIFYING), (domain.UPLOADING_PRIVATE, domain.FAILED),
    (domain.VERIFYING, domain.PUBLISHED), (domain.VERIFYING, domain.UPLOADED_PRIVATE), (domain.VERIFYING, domain.FAILED),
    (domain.FAILED, domain.REPROCESS_REQUESTED),
}


@pytest.mark.parametrize("src,dst", list(itertools.product(domain.STATES, domain.STATES)))
def test_every_transition_pair(src, dst):
    job = make_job(src)
    if (src, dst) in EXPECTED_VALID:
        assert job.transition(dst) == src
        assert job.state == dst
    else:
        with pytest.raises(InvalidTransition):
            job.transition(dst)
        assert job.state == src


def test_terminal_states_have_no_exit():
    for state in (domain.PUBLISHED, domain.CANCELLED, domain.UPLOADED_PRIVATE):
        assert not domain.TRANSITIONS[state]


def test_scope_entry_states():
    assert domain.SCOPE_ENTRY_STATE[domain.SHORTS_ONLY] == domain.GENERATING_ASSETS
    for scope in (domain.FULL, domain.TRANSCRIPT_AND_ANALYSIS, domain.METADATA_ONLY):
        assert domain.SCOPE_ENTRY_STATE[scope] == domain.ANALYZING


def test_idempotency_key_depends_on_every_component():
    base = ("local", "quiz-al-volo", "qav-1", "a" * 64, "v1")
    keys = {domain.idempotency_key(*base)}
    for i, _ in enumerate(base):
        changed = list(base)
        changed[i] = changed[i] + "x"
        keys.add(domain.idempotency_key(*changed))
    assert len(keys) == 6


def test_scrub_removes_secret_fields():
    data = {"a": 1, "upload_session_url": "x", "nested": [{"refresh_token": "y", "ok": 2}]}
    assert domain.scrub(data) == {"a": 1, "nested": [{"ok": 2}]}


def test_job_round_trip_ignores_unknown_fields():
    job = make_job()
    data = job.to_dict() | {"legacy": 1}
    assert Job.from_dict(data) == job
