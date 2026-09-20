"""Shorts planning: manifest segments take precedence; generic candidates come from
the analysis only when the producer sent no manifest. A similarity gate stops
near-identical clips from the same moment."""

from __future__ import annotations

from dataclasses import dataclass, field

from autopublisher.providers.analysis import tokens
from autopublisher.providers.transcription import Transcript

YOUTUBE_SHORT_MAX_MS = 179_000  # under three minutes, with a margin
PRODUCT_MIN_MS = 20_000
PRODUCT_MAX_MS = 45_000
SAFE_TOP = 0.15    # Shorts UI overlays: title/channel at the top
SAFE_BOTTOM = 0.62  # captions must end above the like/comment/description column
SAFE_LEFT = 0.06
SAFE_RIGHT = 0.80


@dataclass
class Caption:
    start_ms: int
    end_ms: int
    text: str
    kind: str = "TEXT"  # HOOK | QUESTION | ANSWER | EXPLANATION | TEXT


@dataclass
class ShortPlan:
    candidate_id: str
    start_ms: int
    end_ms: int
    segment_ids: list[str] = field(default_factory=list)
    crop_region: dict | None = None
    captions: list[Caption] = field(default_factory=list)
    title_hint: str = ""
    origin: str = "manifest"  # manifest | analysis
    priority: int = 50
    warnings: list[dict] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "segment_ids": self.segment_ids, "crop_region": self.crop_region,
                "captions": [c.__dict__ for c in self.captions], "title_hint": self.title_hint,
                "origin": self.origin, "priority": self.priority, "warnings": self.warnings}

    @classmethod
    def from_dict(cls, data: dict) -> ShortPlan:
        return cls(candidate_id=data["candidate_id"], start_ms=data["start_ms"], end_ms=data["end_ms"],
                   segment_ids=list(data.get("segment_ids", [])), crop_region=data.get("crop_region"),
                   captions=[Caption(**c) for c in data.get("captions", [])], title_hint=data.get("title_hint", ""),
                   origin=data.get("origin", "manifest"), priority=data.get("priority", 50),
                   warnings=list(data.get("warnings", [])))


def _captions_from_manifest(manifest: dict, start_ms: int, end_ms: int) -> list[Caption]:
    out = []
    for seg in manifest["segments"]:
        if seg["end_ms"] <= start_ms or seg["start_ms"] >= end_ms or not seg.get("text"):
            continue
        out.append(Caption(max(seg["start_ms"], start_ms) - start_ms, min(seg["end_ms"], end_ms) - start_ms,
                           seg["text"], seg["type"] if seg["type"] in ("HOOK", "QUESTION", "ANSWER", "EXPLANATION") else "TEXT"))
    return out


def _captions_from_transcript(transcript: Transcript | None, start_ms: int, end_ms: int) -> list[Caption]:
    if transcript is None:
        return []
    return [Caption(max(s.start_ms, start_ms) - start_ms, min(s.end_ms, end_ms) - start_ms, s.text)
            for s in transcript.within(start_ms, end_ms) if s.text]


def _product_warnings(plan: ShortPlan) -> list[dict]:
    warnings = []
    if plan.duration_ms < PRODUCT_MIN_MS:
        warnings.append({"code": "SHORT_BELOW_PRODUCT_MIN", "message": f"{plan.duration_ms} ms < {PRODUCT_MIN_MS} ms"})
    if plan.duration_ms > PRODUCT_MAX_MS:
        warnings.append({"code": "SHORT_ABOVE_PRODUCT_MAX", "message": f"{plan.duration_ms} ms > {PRODUCT_MAX_MS} ms"})
    return warnings


def plan_from_manifest(manifest: dict, transcript: Transcript | None, max_shorts: int) -> list[ShortPlan]:
    plans = []
    for cand in sorted(manifest["short_candidates"], key=lambda c: (c["priority"], c["candidate_id"])):
        plan = ShortPlan(candidate_id=cand["candidate_id"], start_ms=cand["start_ms"], end_ms=cand["end_ms"],
                         segment_ids=list(cand["segment_ids"]), crop_region=cand.get("crop_region"),
                         title_hint=cand.get("title_hint", ""), origin="manifest", priority=cand["priority"])
        plan.captions = _captions_from_manifest(manifest, plan.start_ms, plan.end_ms) or \
            _captions_from_transcript(transcript, plan.start_ms, plan.end_ms)
        plan.warnings = _product_warnings(plan)
        if plan.duration_ms > YOUTUBE_SHORT_MAX_MS:
            plan.warnings.append({"code": "SHORT_EXCEEDS_PLATFORM_MAX", "message": "candidate skipped"})
            continue
        plans.append(plan)
    return plans[:max_shorts]


def plan_from_analysis(analysis: dict, transcript: Transcript | None, max_shorts: int) -> list[ShortPlan]:
    plans = []
    for cand in analysis.get("clip_candidates", []):
        plan = ShortPlan(candidate_id=cand["candidate_id"], start_ms=cand["start_ms"], end_ms=cand["end_ms"],
                         title_hint=cand.get("title_hint", ""), origin="analysis")
        plan.captions = _captions_from_transcript(transcript, plan.start_ms, plan.end_ms)
        plan.warnings = _product_warnings(plan)
        if plan.duration_ms > YOUTUBE_SHORT_MAX_MS or plan.duration_ms <= 0:
            continue
        plans.append(plan)
    return plans[:max_shorts]


def overlap_ratio(a: ShortPlan, b: ShortPlan) -> float:
    inter = max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))
    shortest = max(1, min(a.duration_ms, b.duration_ms))
    return inter / shortest


def text_similarity(a: ShortPlan, b: ShortPlan) -> float:
    ta = set().union(*(tokens(c.text) for c in a.captions)) if a.captions else set()
    tb = set().union(*(tokens(c.text) for c in b.captions)) if b.captions else set()
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def similarity_gate(plans: list[ShortPlan], max_overlap: float = 0.5, max_text: float = 0.8) -> tuple[list[ShortPlan], list[dict]]:
    """Drop later candidates that repeat an earlier moment or an earlier text."""
    kept: list[ShortPlan] = []
    dropped: list[dict] = []
    for plan in plans:
        clash = next((k for k in kept if overlap_ratio(k, plan) > max_overlap or text_similarity(k, plan) > max_text), None)
        if clash:
            dropped.append({"code": "NEAR_DUPLICATE_SHORT", "message": f"{plan.candidate_id} repeats {clash.candidate_id}"})
            continue
        kept.append(plan)
    return kept, dropped


def plan_shorts(manifest: dict | None, analysis: dict | None, transcript: Transcript | None,
                max_shorts: int) -> tuple[list[ShortPlan], list[dict]]:
    if manifest is not None:
        plans = plan_from_manifest(manifest, transcript, max_shorts * 2)
    elif analysis is not None:
        plans = plan_from_analysis(analysis, transcript, max_shorts * 2)
    else:
        return [], [{"code": "NO_SHORT_SOURCE", "message": "neither manifest nor analysis available"}]
    kept, dropped = similarity_gate(plans)
    return kept[:max_shorts], dropped
