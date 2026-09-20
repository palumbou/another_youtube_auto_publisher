"""Structured content analysis behind one interface, validated by JSON Schema and
grounded against the transcript and the manifest before anything is stored."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from autopublisher.providers import ProviderError
from autopublisher.providers.transcription import Transcript

PROMPT_VERSION = "2026-09-20.1"

ANALYSIS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "topics", "clip_candidates", "warnings", "title_candidates", "description",
                 "chapters", "hashtags", "tags", "category", "playlist", "thumbnail_concepts",
                 "confidence", "evidence_spans"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
        "topics": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 12},
        "clip_candidates": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["candidate_id", "start_ms", "end_ms", "rationale", "evidence"],
            "properties": {
                "candidate_id": {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"},
                "start_ms": {"type": "integer", "minimum": 0},
                "end_ms": {"type": "integer", "minimum": 1},
                "rationale": {"type": "string", "maxLength": 500},
                "evidence": {"type": "string", "maxLength": 500},
                "title_hint": {"type": "string", "maxLength": 100},
            }}},
        "warnings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["code", "message"],
            "properties": {"code": {"type": "string", "maxLength": 60}, "message": {"type": "string", "maxLength": 500}}}},
        "title_candidates": {"type": "array", "minItems": 3, "maxItems": 3,
                             "items": {"type": "string", "minLength": 5, "maxLength": 100}},
        "description": {"type": "string", "minLength": 1, "maxLength": 5000},
        "chapters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["start_ms", "title"],
            "properties": {"start_ms": {"type": "integer", "minimum": 0}, "title": {"type": "string", "maxLength": 100}}}},
        "hashtags": {"type": "array", "minItems": 1, "maxItems": 3,
                     "items": {"type": "string", "pattern": "^#[^\\s#]{1,99}$"}, "uniqueItems": True},
        "tags": {"type": "array", "maxItems": 15, "items": {"type": "string", "maxLength": 60}},
        "category": {"type": "string", "maxLength": 100},
        "playlist": {"type": "string", "maxLength": 200},
        "thumbnail_concepts": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 200}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_spans": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["source", "ref", "text"],
            "properties": {"source": {"type": "string", "enum": ["transcript", "manifest", "owner"]},
                           "ref": {"type": "string", "maxLength": 128}, "text": {"type": "string", "maxLength": 500}}}},
    },
}

_validator = Draft202012Validator(ANALYSIS_SCHEMA)
STOPWORDS = {"the", "and", "with", "this", "that", "from", "your", "have", "what", "which", "quiz",
             "della", "delle", "dello", "degli", "nella", "nelle", "questa", "questo", "sono", "come",
             "quale", "quali", "video", "domanda", "risposta", "conosci", "sapresti", "ecco", "perché"}


class GroundingError(ValueError):
    pass


class AnalysisValidationError(ValueError):
    pass


@dataclass
class AnalysisInput:
    manifest: dict | None
    transcript: Transcript | None
    probe: dict = field(default_factory=dict)
    language: str = ""
    owner_notes: str = ""
    previous_analysis: dict | None = None


class ContentAnalysisProvider(ABC):
    name = "abstract"
    prompt_version = PROMPT_VERSION

    @abstractmethod
    def analyze(self, request: AnalysisInput) -> dict: ...


def validate_analysis(raw: dict) -> dict:
    errors = sorted(_validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        pointer = "/" + "/".join(str(p) for p in first.absolute_path)
        raise AnalysisValidationError(f"analysis output invalid at {pointer}: {first.message[:200]}")
    for cand in raw["clip_candidates"]:
        if cand["start_ms"] >= cand["end_ms"]:
            raise AnalysisValidationError(f"clip {cand['candidate_id']} has an empty window")
    return raw


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w']+", text.lower()) if len(t) >= 4 and t not in STOPWORDS}


def corpus_tokens(manifest: dict | None, transcript: Transcript | None, owner_notes: str = "") -> set[str]:
    parts = [owner_notes]
    if manifest:
        content = manifest["content"]
        parts += [content.get("title_hint", ""), content["summary"], content["category"],
                  manifest["youtube"].get("playlist_hint", ""), manifest["youtube"].get("category_hint", "")]
        parts += [s.get("text", "") for s in manifest["segments"]]
        parts += [c.get("title_hint", "") for c in manifest["short_candidates"]]
        parts += [h.lstrip("#") for h in manifest["youtube"].get("hashtags_hint", [])]
        parts += [link["title"] for link in content.get("source_links", [])]
    if transcript:
        parts.append(transcript.text)
    return set().union(*(tokens(p) for p in parts if p))


def ground(analysis: dict, manifest: dict | None, transcript: Transcript | None,
           owner_notes: str = "", threshold: float = 0.5) -> list[dict]:
    """Every title and every description sentence must be supported by the corpus.
    Returns warnings for weak support; raises GroundingError for unsupported claims."""
    corpus = corpus_tokens(manifest, transcript, owner_notes)
    if not corpus:
        raise GroundingError("no transcript or manifest to ground the analysis against")
    warnings: list[dict] = []

    def support(text: str) -> float:
        toks = tokens(text)
        if not toks:
            return 1.0
        return len(toks & corpus) / len(toks)

    for i, title in enumerate(analysis["title_candidates"]):
        score = support(title)
        if score < threshold:
            raise GroundingError(f"title candidate {i} is not supported by transcript or manifest")
        if score < 0.75:
            warnings.append({"code": "WEAK_TITLE_SUPPORT", "message": f"title {i} support {score:.2f}"})
    for j, sentence in enumerate(re.split(r"(?<=[.!?])\s+", analysis["description"])):
        if not sentence.strip() or sentence.strip().startswith(("http://", "https://")):
            continue
        score = support(sentence)
        if score < threshold:
            raise GroundingError(f"description sentence {j} is not supported by transcript or manifest")
    duration = manifest["source"]["duration_ms"] if manifest else None
    for cand in analysis["clip_candidates"]:
        if duration is not None and cand["end_ms"] > duration:
            raise GroundingError(f"clip {cand['candidate_id']} exceeds the source duration")
    return warnings


class ManifestAnalysisProvider(ContentAnalysisProvider):
    """Local/mock provider: derives every proposal from manifest and transcript, so
    the output is grounded by construction and the review flow can be exercised."""

    name = "manifest-mock"

    def analyze(self, request: AnalysisInput) -> dict:
        m = request.manifest
        if not m:
            raise ProviderError("the mock provider needs a manifest")
        content = m["content"]
        questions = [s for s in m["segments"] if s["type"] == "QUESTION" and s.get("text")]
        answers = {s.get("question_id"): s.get("text", "") for s in m["segments"] if s["type"] == "ANSWER"}
        explanations = {s.get("question_id"): s.get("text", "") for s in m["segments"] if s["type"] == "EXPLANATION"}
        first_q = questions[0]["text"] if questions else content.get("title_hint", content["category"])
        title_hint = content.get("title_hint") or first_q
        titles = [title_hint[:100], first_q[:100], f"{content['category'].capitalize()}: {first_q}"[:100]]
        desc_parts = [content["summary"]]
        for q in questions:
            qid = q.get("question_id")
            if answers.get(qid):
                desc_parts.append(f"{q['text']} {answers[qid]}.")
            if explanations.get(qid):
                desc_parts.append(explanations[qid])
        for link in content.get("source_links", []):
            desc_parts.append(f"{link['title']}: {link['url']}")
        chapters = [{"start_ms": q["start_ms"], "title": q["text"][:100]} for q in questions]
        hashtags = m["youtube"].get("hashtags_hint") or [f"#{re.sub(r'[^a-z0-9]', '', content['category'].lower())}"]
        tags = [content["category"], f"quiz {content['category']}", "cultura generale"]
        tags += [t for t in (content.get("title_hint", "").lower(),) if t]
        candidates = [{
            "candidate_id": c["candidate_id"], "start_ms": c["start_ms"], "end_ms": c["end_ms"],
            "rationale": "declared by the producer manifest", "evidence": f"manifest short_candidates/{i}",
            "title_hint": c.get("title_hint", "")[:100],
        } for i, c in enumerate(m["short_candidates"])]
        evidence = [{"source": "manifest", "ref": f"segments/{i}", "text": s.get("text", "")[:500]}
                    for i, s in enumerate(m["segments"]) if s.get("text")]
        if request.transcript:
            evidence.append({"source": "transcript", "ref": "full", "text": request.transcript.text[:500]})
        warnings = []
        if request.transcript and request.transcript.language and request.transcript.language != m["language"]:
            warnings.append({"code": "LANGUAGE_MISMATCH",
                             "message": f"transcript {request.transcript.language} vs manifest {m['language']}"})
        return {
            "summary": content["summary"][:1000], "topics": [content["category"]] + [q["text"][:80] for q in questions][:5],
            "clip_candidates": candidates, "warnings": warnings, "title_candidates": titles,
            "description": " ".join(desc_parts)[:5000], "chapters": chapters, "hashtags": hashtags[:3],
            "tags": tags[:15], "category": m["youtube"].get("category_hint", "Education"),
            "playlist": m["youtube"].get("playlist_hint", ""),
            "thumbnail_concepts": [f"Large question text: {first_q[:80]}", "Answer reveal frame with the correct option"],
            "confidence": 0.9, "evidence_spans": evidence[:50],
        }


class FailingAnalysisProvider(ContentAnalysisProvider):
    name = "failing"

    def analyze(self, request: AnalysisInput) -> dict:
        raise ProviderError("analysis provider unavailable")


class UngroundedAnalysisProvider(ManifestAnalysisProvider):
    """Test double that hallucinates a title claim."""

    name = "ungrounded-mock"

    def analyze(self, request: AnalysisInput) -> dict:
        out = super().analyze(request)
        out["title_candidates"][0] = "Exclusive interview with a famous astronaut about Mars colonies"
        return out


class BedrockAnalysisProvider(ContentAnalysisProvider):
    """Amazon Bedrock (Converse API with a forced tool call) behind the same interface."""

    name = "bedrock"

    def __init__(self, model_id: str | None = None, client=None, max_tokens: int = 4000):
        import boto3

        self.model_id = model_id or os.environ.get("MODEL_ID", "")
        self.client = client or boto3.client("bedrock-runtime")
        self.max_tokens = max_tokens
        self.last_usage: dict = {}

    def build_prompt(self, request: AnalysisInput) -> str:
        lines = [
            f"Prompt version {self.prompt_version}. You prepare a YouTube publication for review by the channel owner.",
            "Use only facts present in the manifest and the transcript below. Never invent names, claims or numbers.",
            "Return the structured plan through the submit_analysis tool. Titles must be three genuinely",
            "different candidates that describe the actual content. Hashtags: one to three. Tags: few, useful",
            "spelling variants only. Do not promise rankings or use clickbait.",
        ]
        if request.manifest:
            lines += ["", "MANIFEST (authoritative for question boundaries and rights):",
                      json.dumps(request.manifest, ensure_ascii=False)[:20000]]
        if request.transcript:
            lines += ["", f"TRANSCRIPT ({request.transcript.language}):",
                      json.dumps([s.__dict__ for s in request.transcript.segments], ensure_ascii=False)[:12000]]
        if request.probe:
            lines += ["", "MEDIA PROBE:", json.dumps(request.probe)[:2000]]
        if request.owner_notes:
            lines += ["", "OWNER NOTES (priority, but still no invented facts):", request.owner_notes[:2000]]
        return "\n".join(lines)

    def analyze(self, request: AnalysisInput) -> dict:
        tool = {"toolSpec": {"name": "submit_analysis", "description": "Submit the structured analysis.",
                             "inputSchema": {"json": {k: v for k, v in ANALYSIS_SCHEMA.items() if k != "$schema"}}}}
        response = self.client.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": self.build_prompt(request)}]}],
            toolConfig={"tools": [tool], "toolChoice": {"tool": {"name": "submit_analysis"}}},
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": 0.2},
        )
        self.last_usage = response.get("usage", {})
        for block in response["output"]["message"]["content"]:
            if "toolUse" in block:
                return block["toolUse"]["input"]
        raise ProviderError("model did not return a structured analysis")
