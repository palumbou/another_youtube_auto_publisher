from autopublisher.pipeline import job_id_for_key


class TestJobIdForKey:
    def test_stable(self):
        assert job_id_for_key("incoming/My Video.mp4") == job_id_for_key("incoming/My Video.mp4")

    def test_distinct_for_different_keys(self):
        assert job_id_for_key("a/video.mp4") != job_id_for_key("b/video.mp4")

    def test_sanitized_and_lowercase(self):
        job_id = job_id_for_key("incoming/Città & Räume (2026).mp4")
        stem = job_id.rsplit("-", 1)[0]
        assert stem == stem.lower()
        assert all(c.isalnum() or c == "-" for c in job_id)

    def test_long_names_truncated(self):
        job_id = job_id_for_key("incoming/" + "x" * 300 + ".mp4")
        # 60-char stem + "-" + 8-char digest
        assert len(job_id) == 69
