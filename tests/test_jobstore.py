import pytest

from autopublisher import domain
from autopublisher.domain import Asset, AuditEvent, Publication, Revision, Schedule
from autopublisher.jobstore import AlreadyExists, ConflictError, LocalJobStore
from tests.test_domain import make_job


@pytest.fixture
def store(tmp_path):
    return LocalJobStore(tmp_path / "state")


def test_create_is_conditional(store):
    job = make_job()
    store.create_job(job)
    with pytest.raises(AlreadyExists):
        store.create_job(make_job())
    assert store.get_job(job.project_key, job.job_id).idempotency_key == "k"


def test_update_uses_optimistic_locking(store):
    job = make_job()
    store.create_job(job)
    first = store.get_job(job.project_key, job.job_id)
    second = store.get_job(job.project_key, job.job_id)
    first.transition(domain.VALIDATING)
    store.update_job(first, expected_version=1)
    assert first.version == 2
    second.transition(domain.VALIDATING)
    with pytest.raises(ConflictError):
        store.update_job(second, expected_version=1)
    assert store.get_job(job.project_key, job.job_id).version == 2


def test_revisions_are_immutable_except_review_fields(store):
    rev = Revision(job_id="qav-1", project_key="quiz-al-volo", number=1,
                   shorts=[Asset(asset_id="short-01", type=domain.SHORT, key="k")])
    store.put_revision(rev)
    with pytest.raises(AlreadyExists):
        store.put_revision(rev)
    rev.shorts[0].review_status = domain.REVIEW_APPROVED
    rev.shorts[0].key = "tampered"
    rev.master_review_status = domain.REVIEW_REJECTED
    store.update_revision_review(rev)
    stored = store.get_revision("quiz-al-volo", "qav-1", 1)
    assert stored.shorts[0].review_status == domain.REVIEW_APPROVED
    assert stored.shorts[0].key == "k"
    assert stored.master_review_status == domain.REVIEW_REJECTED
    assert [r.number for r in store.list_revisions("quiz-al-volo", "qav-1")] == [1]


def test_audit_is_append_only_and_sequenced(store):
    for i in range(3):
        store.append_audit(AuditEvent(project_key="p", job_id="j", action=f"A{i}", actor="t"))
    events = store.list_audit("p", "j")
    assert [e.sequence for e in events] == [1, 2, 3]
    assert [e.action for e in events] == ["A0", "A1", "A2"]


def test_schedule_and_publication_conditionals(store):
    sched = Schedule(schedule_id="s1", project_key="p", job_id="j", revision=1, asset_id="a",
                     publish_at_utc="2026-10-01T10:00:00Z", owner_timezone="Europe/Rome")
    store.create_schedule(sched)
    with pytest.raises(AlreadyExists):
        store.create_schedule(sched)
    sched.status = "QUEUED"
    store.update_schedule(sched, expected_version=1)
    with pytest.raises(ConflictError):
        store.update_schedule(sched, expected_version=1)
    assert store.list_schedules("QUEUED")[0].version == 2
    pub = Publication(publication_id="p#j#1#a", project_key="p", job_id="j", revision=1, asset_id="a")
    store.create_publication(pub)
    with pytest.raises(AlreadyExists):
        store.create_publication(pub)
    assert store.get_publication("p#j#1#a").youtube_video_id == ""
