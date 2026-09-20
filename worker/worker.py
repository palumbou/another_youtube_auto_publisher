"""Fargate worker: the media-heavy half of the pipeline, driven by environment variables.

Modes (env MODE):
  process — run one processing revision for a job (validate, transcribe, analyse,
            render, verify) with the AWS providers and stop at AWAITING_REVIEW.
            Env: BUCKET, TABLE_PREFIX, PROJECT_KEY, JOB_ID, SCOPE, REASON,
            TRANSCRIPTION_PROVIDER (transcribe|manifest), ANALYSIS_PROVIDER (bedrock|manifest).
  render  — render a plan file locally (development aid):
            worker.py render --local <video> --workdir <dir with plan.json>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def build_processor():
    from autopublisher.config import Settings
    from autopublisher.jobstore import DynamoJobStore
    from autopublisher.objectstore import S3ObjectStore
    from autopublisher.processing import Processor
    from autopublisher.providers.analysis import BedrockAnalysisProvider, ManifestAnalysisProvider
    from autopublisher.providers.transcription import (
        ManifestTranscriptionProvider,
        TranscribeProvider,
    )

    settings = Settings.from_env()
    store = S3ObjectStore(os.environ["BUCKET"])
    store.uri = lambda key: f"s3://{os.environ['BUCKET']}/{key}"  # type: ignore[attr-defined]
    jobs = DynamoJobStore(os.environ["TABLE_PREFIX"])
    transcription = (TranscribeProvider(output_bucket=os.environ["BUCKET"])
                     if os.environ.get("TRANSCRIPTION_PROVIDER", "transcribe") == "transcribe"
                     else ManifestTranscriptionProvider())
    analysis = (BedrockAnalysisProvider() if os.environ.get("ANALYSIS_PROVIDER", "bedrock") == "bedrock"
                else ManifestAnalysisProvider())
    workdir = Path("/work") if Path("/work").is_dir() else None
    return settings, store, jobs, Processor(settings, store, jobs, transcription, analysis, workdir=workdir)


def process_main() -> None:
    from autopublisher.domain import FULL

    _, _, jobs, processor = build_processor()
    job = jobs.get_job(os.environ["PROJECT_KEY"], os.environ["JOB_ID"])
    if job is None:
        raise SystemExit("job not found")
    scope = os.environ.get("SCOPE") or job.reprocess_scope or FULL
    revision = processor.run(job, scope=scope, reason=os.environ.get("REASON", ""))
    print(json.dumps({"job": job.pk, "revision": revision.number, "shorts": len(revision.shorts),
                      "warnings": len(revision.warnings)}), flush=True)


def render_local(video: Path, workdir: Path) -> None:
    from autopublisher.media import available_encoder, ffprobe
    from autopublisher.processing import render_plan
    from autopublisher.shorts import ShortPlan

    plan = json.loads((workdir / "plan.json").read_text())
    info = ffprobe(video)
    output = render_plan(video, info, [ShortPlan.from_dict(p) for p in plan["shorts"]],
                         plan.get("thumbnails", []), workdir / "out", available_encoder())
    (workdir / "commands.json").write_text(output.log.to_json())
    print(json.dumps({"shorts": [a.__dict__ for a in output.shorts], "thumbnails": [a.__dict__ for a in output.thumbnails]},
                     indent=2, default=str))


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else os.environ.get("MODE", "")
    if mode == "render" and "--local" in args:
        video = Path(args[args.index("--local") + 1])
        workdir = Path(args[args.index("--workdir") + 1]) if "--workdir" in args else video.parent
        render_local(video, workdir)
    elif mode == "process":
        process_main()
    else:
        raise SystemExit(f"unknown mode: {mode}")
