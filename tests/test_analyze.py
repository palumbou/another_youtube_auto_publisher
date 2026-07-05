from autopublisher.analyze import build_prompt, clean_plan
from autopublisher.models import MAX_SHORT_SECONDS


def raw_short(start, end, i=1):
    return {
        "start": start, "end": end,
        "title": f"Title {i}", "description": f"Desc {i}",
        "tags": ["tag"], "reason": f"Reason {i}",
    }


PROBE = {
    "duration": 600.0, "width": 1920, "height": 1080, "fps": 30.0,
    "has_audio": True, "has_speech": True,
    "mosaic_interval_s": 2.5, "mosaic_grid": "4x6",
    "scene_changes": [10.0, 42.5], "loudness_db_per_s": [-20.0, -25.0],
    "loudness_step_s": 1,
}


class TestCleanPlan:
    def test_clamps_to_video_bounds(self):
        plan = clean_plan({"main": {}, "shorts": [raw_short(-5, 700)]}, 600, 1, 6)
        s = plan["shorts"][0]
        assert s["start"] == 0.0
        # clamped to duration first, then to the Shorts limit
        assert s["end"] == MAX_SHORT_SECONDS

    def test_drops_tiny_segments(self):
        plan = clean_plan({"main": {}, "shorts": [raw_short(10, 12)]}, 600, 1, 6)
        assert plan["shorts"] == []

    def test_sorts_by_start_and_assigns_ids(self):
        plan = clean_plan(
            {"main": {}, "shorts": [raw_short(100, 150, 2), raw_short(10, 60, 1)]},
            600, 1, 6,
        )
        assert [s["short_id"] for s in plan["shorts"]] == ["short-01", "short-02"]
        assert plan["shorts"][0]["start"] == 10

    def test_caps_number_of_shorts(self):
        shorts = [raw_short(i * 100, i * 100 + 50, i) for i in range(5)]
        plan = clean_plan({"main": {}, "shorts": shorts}, 600, 1, 3)
        assert len(plan["shorts"]) == 3

    def test_sanitizes_metadata(self):
        s = raw_short(0, 30)
        s["title"] = "<script>Bad</script>"
        plan = clean_plan({"main": {"title": "M<>ain", "description": "d", "tags": ["t"]},
                           "shorts": [s]}, 600, 1, 6)
        assert "<" not in plan["shorts"][0]["title"]
        assert plan["main"]["title"] == "Main"

    def test_empty_plan_survives(self):
        plan = clean_plan({}, 600, 1, 6)
        assert plan == {"main": {"title": "", "description": "", "tags": []}, "shorts": []}


class TestBuildPrompt:
    def test_includes_probe_facts(self):
        prompt = build_prompt(PROBE, "", "", "", 2, 6)
        assert "600s" in prompt
        assert "4x6" in prompt
        assert "scene changes" in prompt.lower()

    def test_transcript_and_language(self):
        prompt = build_prompt(PROBE, "hello world", "it", "", 2, 6)
        assert "hello world" in prompt
        assert "language: it" in prompt

    def test_user_prompt_marked_as_priority(self):
        prompt = build_prompt(PROBE, "", "", "focus on the drone footage", 2, 6)
        assert "focus on the drone footage" in prompt
        assert "take priority" in prompt

    def test_shorts_range(self):
        prompt = build_prompt(PROBE, "", "", "", 3, 5)
        assert "between 3 and 5" in prompt
