"""Analysis step: ask Claude (Bedrock, vision) to pick meaningful Short segments and
write English titles/descriptions/tags for the Shorts and the main video.

Input event (from Step Functions):
  {"job_id": ..., "mode": "full" | "reanalyze", "prompt": optional user guidance,
   "transcript_key": optional}
Output: {"job_id", "plan_key", "num_shorts"}
"""

from __future__ import annotations

import json
import os

import boto3

from autopublisher import storage
from autopublisher.models import (
    MAX_SHORT_SECONDS,
    STATUS_ANALYZING,
    Short,
    sanitize_description,
    sanitize_tags,
    sanitize_title,
)

MAX_TRANSCRIPT_CHARS = 8000

PLAN_TOOL = {
    "toolSpec": {
        "name": "submit_plan",
        "description": "Submit the publishing plan for this video.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "main": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title", "description", "tags"],
                    },
                    "shorts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "number", "description": "seconds from video start"},
                                "end": {"type": "number"},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "reason": {"type": "string", "description": "why this segment works as a Short"},
                            },
                            "required": ["start", "end", "title", "description", "tags", "reason"],
                        },
                    },
                },
                "required": ["main", "shorts"],
            }
        },
    }
}


def build_prompt(probe: dict, transcript: str, language: str, user_prompt: str,
                 min_shorts: int, max_shorts: int) -> str:
    lines = [
        ("You are planning the YouTube publication of a raw video (no editing allowed: "
        "segments are published exactly as they are)."),
        "",
        (f"The attached images are frame mosaics sampled every {probe['mosaic_interval_s']}s, "
        f"grid {probe['mosaic_grid']} read left-to-right then top-to-bottom, in chronological "
        "order. Each tile has its timestamp burned in at the bottom-left."),
        "",
        (f"Video: {probe['duration']:.0f}s, {probe['width']}x{probe['height']}, "
        f"{probe['fps']}fps, audio: {probe['has_audio']}, speech detected: {probe['has_speech']}."),
    ]
    if probe.get("scene_changes"):
        scenes = ", ".join(f"{t:.0f}" for t in probe["scene_changes"][:200])
        lines.append(f"Hard visual scene changes at (seconds): {scenes}")
    if probe.get("loudness_db_per_s"):
        step = probe.get("loudness_step_s", 1)
        curve = " ".join(str(int(v)) for v in probe["loudness_db_per_s"])
        lines.append(f"Audio RMS level in dB, one value every {step}s (lower = quieter): {curve}")
    if transcript:
        lines += ["", f"Speech transcript (language: {language or 'unknown'}):", transcript]
    lines += [
        "",
        (f"Task: choose between {min_shorts} and {max_shorts} segments that work as YouTube "
        "Shorts — self-contained, visually interesting moments a viewer without context "
        "would watch to the end. Prefer starting/ending near scene changes or natural "
        "pauses. Segments must not overlap. Ideal length 20-90 seconds, hard maximum "
        f"{MAX_SHORT_SECONDS} seconds. If the video is monotonous, pick fewer, shorter segments."),
        "",
        ("Also produce metadata for the FULL video. All titles, descriptions and tags must "
        "be in ENGLISH regardless of the video language. Descriptions: 1-3 sentences, "
        "factual, no hashtags spam, no clickbait. Tags: 5-15 relevant terms per item."),
    ]
    if user_prompt:
        lines += ["", "Extra instructions from the channel owner (they take priority):", user_prompt]
    lines += ["", "Reply by calling submit_plan."]
    return "\n".join(lines)


def call_bedrock(mosaic_images: list[bytes], prompt: str) -> dict:
    client = boto3.client("bedrock-runtime")
    content = [{"image": {"format": "jpeg", "source": {"bytes": img}}} for img in mosaic_images]
    content.append({"text": prompt})
    response = client.converse(
        modelId=os.environ["MODEL_ID"],
        messages=[{"role": "user", "content": content}],
        toolConfig={"tools": [PLAN_TOOL], "toolChoice": {"tool": {"name": "submit_plan"}}},
        inferenceConfig={"maxTokens": 4000},
    )
    for block in response["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"]
    raise RuntimeError("model did not return a plan")


def clean_plan(raw: dict, duration: float, min_shorts: int, max_shorts: int) -> dict:
    """Clamp segments to the video bounds and YouTube limits, sanitize all metadata."""
    shorts = []
    for i, s in enumerate(sorted(raw.get("shorts", []), key=lambda x: float(x["start"]))):
        start = max(0.0, float(s["start"]))
        end = min(float(s["end"]), duration)
        if end - start > MAX_SHORT_SECONDS:
            end = start + MAX_SHORT_SECONDS
        if end - start < 3:
            continue
        shorts.append({
            "short_id": f"short-{i + 1:02d}",
            "start": round(start, 2),
            "end": round(end, 2),
            "title": sanitize_title(s.get("title", "")),
            "description": sanitize_description(s.get("description", "")),
            "tags": sanitize_tags(s.get("tags", [])),
            "reason": s.get("reason", "")[:500],
        })
    main = raw.get("main", {})
    return {
        "main": {
            "title": sanitize_title(main.get("title", "")),
            "description": sanitize_description(main.get("description", "")),
            "tags": sanitize_tags(main.get("tags", [])),
        },
        "shorts": shorts[:max_shorts],
    }


def load_transcript(s3, bucket: str, key: str) -> tuple[str, str]:
    """Returns (text, language_code) from an Amazon Transcribe output file."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return "", ""
    data = json.loads(body)
    results = data.get("results", {})
    text = " ".join(t["transcript"] for t in results.get("transcripts", []) if t.get("transcript"))
    language = results.get("language_code", "")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + " [...truncated]"
    return text, language


def handler(event, context):
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    min_shorts = int(os.environ.get("MIN_SHORTS", "2"))
    max_shorts = int(os.environ.get("MAX_SHORTS", "6"))

    job_id = event["job_id"]
    job = storage.load_job(job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")
    prefix = f"work/{job_id}"

    probe = json.loads(s3.get_object(Bucket=bucket, Key=f"{prefix}/probe.json")["Body"].read())
    transcript, detected_language = "", ""
    transcript_key = event.get("transcript_key") or f"{prefix}/transcript.json"
    if probe.get("has_speech"):
        transcript, detected_language = load_transcript(s3, bucket, transcript_key)

    mosaics = [
        s3.get_object(Bucket=bucket, Key=f"{prefix}/mosaics/{name}")["Body"].read()
        for name in probe.get("mosaic_files", [])
    ]

    user_prompt = event.get("prompt") or job.prompt
    language = job.language_override or detected_language
    raw = call_bedrock(mosaics, build_prompt(probe, transcript, language, user_prompt,
                                             min_shorts, max_shorts))
    plan = clean_plan(raw, probe["duration"], min_shorts, max_shorts)

    plan_key = f"{prefix}/plan.json"
    s3.put_object(Bucket=bucket, Key=plan_key, Body=json.dumps(plan, indent=2).encode(),
                  ContentType="application/json")

    job.status = STATUS_ANALYZING
    job.has_speech = bool(probe.get("has_speech"))
    job.language = language
    job.prompt = user_prompt or ""
    job.main.title = plan["main"]["title"]
    job.main.description = plan["main"]["description"]
    job.main.tags = plan["main"]["tags"]
    job.shorts = [Short(**{k: v for k, v in s.items()}) for s in plan["shorts"]]
    storage.save_job(job)

    return {"job_id": job_id, "plan_key": plan_key, "num_shorts": len(plan["shorts"])}
