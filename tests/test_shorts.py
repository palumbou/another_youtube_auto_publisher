from autopublisher import shorts
from autopublisher.shorts import Caption, ShortPlan, plan_shorts, similarity_gate
from tests import helpers


def test_manifest_candidates_take_precedence_and_keep_the_explanation():
    manifest = helpers.tiny_manifest()
    analysis = {"clip_candidates": [{"candidate_id": "guess", "start_ms": 0, "end_ms": 1000}]}
    plans, dropped = plan_shorts(manifest, analysis, None, max_shorts=4)
    assert [p.candidate_id for p in plans] == ["short-01"]
    assert plans[0].origin == "manifest"
    kinds = [c.kind for c in plans[0].captions]
    assert kinds == ["HOOK", "QUESTION", "ANSWER", "EXPLANATION"]
    assert plans[0].captions[-1].end_ms == 1900
    assert dropped == []
    assert {w["code"] for w in plans[0].warnings} == {"SHORT_BELOW_PRODUCT_MIN"}


def test_generic_candidates_only_without_manifest():
    analysis = {"clip_candidates": [{"candidate_id": "c1", "start_ms": 0, "end_ms": 30000},
                                    {"candidate_id": "c2", "start_ms": 1000, "end_ms": 31000}]}
    plans, dropped = plan_shorts(None, analysis, None, max_shorts=4)
    assert [p.candidate_id for p in plans] == ["c1"]
    assert dropped[0]["code"] == "NEAR_DUPLICATE_SHORT"


def test_platform_maximum_is_enforced():
    manifest = helpers.tiny_manifest()
    manifest["source"]["duration_ms"] = 400000
    manifest["short_candidates"][0]["end_ms"] = 200000
    plans, _ = plan_shorts(manifest, None, None, 4)
    assert plans == []


def test_similarity_gate_on_text():
    a = ShortPlan("a", 0, 30000, captions=[Caption(0, 1000, "Qual è il fiume più lungo d'Italia? Il Po")])
    b = ShortPlan("b", 60000, 90000, captions=[Caption(0, 1000, "Qual è il fiume più lungo d'Italia? Il Po")])
    kept, dropped = similarity_gate([a, b])
    assert [p.candidate_id for p in kept] == ["a"] and dropped


def test_per_job_limit():
    manifest = helpers.tiny_manifest()
    manifest["short_candidates"] = [dict(manifest["short_candidates"][0], candidate_id=f"s{i}", priority=i,
                                         start_ms=0, end_ms=1900) for i in range(1, 6)]
    plans, _ = plan_shorts(manifest, None, None, max_shorts=2)
    assert len(plans) <= 2
    assert shorts.SAFE_BOTTOM < 0.7
