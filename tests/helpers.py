"""Shared helpers: build a valid job directory around the 2-second fixture video."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from autopublisher.config import Settings
from autopublisher.contract import CONTRACT_DIR

TINY = Path(__file__).parent / "fixtures" / "tiny.mp4"
PROJECT = "quiz-al-volo"

SEGMENTS = [
    ("hook-01", "HOOK", 0, 400, None, "Pronti per una domanda?"),
    ("question-01", "QUESTION", 400, 900, "geo-it-0001", "Qual è il fiume più lungo d'Italia?"),
    ("countdown-01", "COUNTDOWN", 900, 1200, "geo-it-0001", None),
    ("answer-01", "ANSWER", 1200, 1500, "geo-it-0001", "Il Po"),
    ("explanation-01", "EXPLANATION", 1500, 1900, "geo-it-0001",
     "Il Po nasce dal Monviso e scorre per 652 chilometri fino all'Adriatico."),
    ("outro-01", "OUTRO", 1900, 2000, None, None),
]


def settings(**overrides) -> Settings:
    base = {"environment": "local", "local_mode": True,
                "cutover_at": datetime(2026, 1, 1, tzinfo=UTC), "allowed_projects": (PROJECT,)}
    base.update(overrides)
    return Settings(**base)


def tiny_manifest(job_id: str = "qav-test-0001", sha256: str | None = None) -> dict:
    base = json.loads((CONTRACT_DIR / "1.0.0" / "fixtures" / "valid.json").read_text())
    manifest = copy.deepcopy(base)
    manifest["job_id"] = job_id
    manifest["created_at"] = "2026-09-20T10:00:00Z"
    manifest["source"].update({
        "file_name": "master.mp4", "sha256": sha256 or hashlib.sha256(TINY.read_bytes()).hexdigest(),
        "duration_ms": 2000, "width": 320, "height": 240, "frame_rate": 25,
    })
    manifest["content"]["title_hint"] = "Quanto conosci i fiumi italiani?"
    manifest["content"]["summary"] = "Una domanda sul fiume più lungo d'Italia, con risposta e spiegazione."
    segments = []
    for sid, typ, start, end, qid, text in SEGMENTS:
        seg = {"segment_id": sid, "type": typ, "start_ms": start, "end_ms": end}
        if qid:
            seg["question_id"] = qid
        if text:
            seg["text"] = text
        if typ in ("HOOK", "QUESTION"):
            seg["region_of_interest"] = {"x": 0.2, "y": 0.05, "width": 0.6, "height": 0.9}
            seg["text_safe_area"] = {"x": 0.15, "y": 0.12, "width": 0.7, "height": 0.7}
        segments.append(seg)
    manifest["segments"] = segments
    manifest["short_candidates"] = [{
        "candidate_id": "short-01",
        "segment_ids": ["hook-01", "question-01", "countdown-01", "answer-01", "explanation-01"],
        "start_ms": 0, "end_ms": 1900, "priority": 1, "target_aspect_ratio": "9:16",
        "title_hint": "Qual è il fiume più lungo d'Italia?",
        "crop_region": {"x": 0.2, "y": 0, "width": 0.6, "height": 1}, "must_include_explanation": True,
    }]
    manifest["producer"]["git_commit"] = "b" * 40
    return manifest


def make_job_dir(bucket_root: Path, job_id: str = "qav-test-0001", *, manifest: dict | None = None,
                 with_ready: bool = True, project: str = PROJECT, extra: dict[str, bytes] | None = None) -> Path:
    prefix = bucket_root / "incoming" / project / job_id
    (prefix / "source").mkdir(parents=True, exist_ok=True)
    (prefix / "manifest").mkdir(parents=True, exist_ok=True)
    (prefix / "source" / "master.mp4").write_bytes(TINY.read_bytes())
    if manifest is not False:
        data = manifest if manifest is not None else tiny_manifest(job_id)
        (prefix / "manifest" / "video-job-manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    for rel, content in (extra or {}).items():
        target = prefix / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if with_ready:
        (prefix / "READY").write_bytes(b"")
    return prefix


def ready_key(job_id: str = "qav-test-0001", project: str = PROJECT) -> str:
    return f"incoming/{project}/{job_id}/READY"
