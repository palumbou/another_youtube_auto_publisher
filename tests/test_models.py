from autopublisher.models import (
    MAX_TAGS_TOTAL_LEN,
    MAX_TITLE_LEN,
    sanitize_description,
    sanitize_tags,
    sanitize_title,
)


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
