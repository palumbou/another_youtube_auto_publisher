from autopublisher.models import (
    MAX_SHORT_SECONDS,
    MAX_TAGS_TOTAL_LEN,
    MAX_TITLE_LEN,
    STATUS_ANALYZING,
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_PENDING_REVIEW,
    STATUS_PUBLISHING,
    Job,
    Short,
    next_short_to_publish,
    sanitize_description,
    sanitize_tags,
    sanitize_title,
    short_description_with_link,
)

import pytest


def make_job(job_id="job-1", **kwargs) -> Job:
    return Job(job_id=job_id, video_key=f"incoming/{job_id}.mp4", **kwargs)


class TestTransitions:
    def test_happy_path(self):
        job = make_job()
        for status in (STATUS_PENDING_REVIEW, STATUS_APPROVED, STATUS_PUBLISHING, STATUS_DONE):
            job.transition(status)
        assert job.status == STATUS_DONE

    def test_invalid_transition_raises(self):
        job = make_job()
        with pytest.raises(ValueError):
            job.transition(STATUS_DONE)

    def test_reanalyze_from_review(self):
        job = make_job(status=STATUS_PENDING_REVIEW)
        job.transition(STATUS_ANALYZING)
        assert job.status == STATUS_ANALYZING


class TestShort:
    def test_validate_ok(self):
        assert Short("s1", start=10, end=40).validate() == []

    def test_validate_end_before_start(self):
        problems = Short("s1", start=40, end=10).validate()
        assert any("must be after" in p for p in problems)

    def test_validate_too_long(self):
        problems = Short("s1", start=0, end=MAX_SHORT_SECONDS + 1).validate()
        assert any("exceeds" in p for p in problems)


class TestSanitizers:
    def test_title_strips_angle_brackets_and_truncates(self):
        assert sanitize_title("<b>Hello</b>") == "bHello/b"
        assert len(sanitize_title("x" * 500)) == MAX_TITLE_LEN

    def test_description_truncates(self):
        assert len(sanitize_description("y" * 6000)) == 5000

    def test_tags_dedup_case_insensitive(self):
        assert sanitize_tags(["Cats", "cats", "dogs"]) == ["Cats", "dogs"]

    def test_tags_respect_total_budget(self):
        tags = sanitize_tags([f"tag-{i:02d}" + "x" * 44 for i in range(30)])
        # each tag costs len + 1: floor(470 / 51) = 9 fit
        assert len(tags) == 9
        assert sum(len(t) + 1 for t in tags) <= MAX_TAGS_TOTAL_LEN

    def test_tags_with_spaces_cost_quotes(self):
        # 233 chars each; with quotes+separator = 236 -> only one fits in 470
        tag_a, tag_b = "a " + "x" * 231, "b " + "y" * 231
        assert sanitize_tags([tag_a, tag_b]) == [tag_a]

    def test_empty_tags_dropped(self):
        assert sanitize_tags(["", "  ", "<>", "ok"]) == ["ok"]


class TestDescriptionWithLink:
    def test_appends_link(self):
        short = Short("s1", 0, 10, description="A moment.")
        text = short_description_with_link(short, "https://youtu.be/abc")
        assert text == "A moment.\n\nFull video: https://youtu.be/abc"

    def test_no_link(self):
        short = Short("s1", 0, 10, description="A moment.")
        assert short_description_with_link(short, "") == "A moment."


class TestMainVideoLink:
    def test_prefers_existing_id(self):
        job = make_job(existing_youtube_id="ext123")
        job.main.youtube_id = "own456"
        assert job.main_video_link() == "https://youtu.be/ext123"

    def test_empty_without_ids(self):
        assert make_job().main_video_link() == ""


class TestNextShortToPublish:
    def test_none_when_no_publishing_jobs(self):
        assert next_short_to_publish([make_job(status=STATUS_APPROVED)]) is None

    def test_oldest_job_first_in_segment_order(self):
        old = make_job("old", status=STATUS_PUBLISHING, created_at="2026-01-01T00:00:00Z")
        old.shorts = [Short("s1", 0, 10, youtube_id="done"), Short("s2", 20, 30)]
        new = make_job("new", status=STATUS_PUBLISHING, created_at="2026-06-01T00:00:00Z")
        new.shorts = [Short("s1", 0, 10)]
        job, short = next_short_to_publish([new, old])
        assert (job.job_id, short.short_id) == ("old", "s2")

    def test_fully_published_job_is_skipped(self):
        done = make_job("done", status=STATUS_PUBLISHING, created_at="2026-01-01T00:00:00Z")
        done.shorts = [Short("s1", 0, 10, youtube_id="x")]
        assert next_short_to_publish([done]) is None


class TestRoundTrip:
    def test_to_item_from_item(self):
        job = make_job(created_at="2026-07-01T00:00:00Z")
        job.shorts = [Short("s1", 1.5, 42.0, title="T", tags=["a"])]
        job.main.title = "Main"
        restored = Job.from_item(job.to_item())
        assert restored == job

    def test_from_item_ignores_unknown_fields(self):
        item = make_job().to_item()
        item["legacy_field"] = "whatever"
        assert Job.from_item(item).job_id == "job-1"

    def test_all_shorts_published_requires_shorts(self):
        job = make_job()
        assert not job.all_shorts_published()
        job.shorts = [Short("s1", 0, 10, youtube_id="x")]
        assert job.all_shorts_published()
