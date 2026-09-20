"""Run one processing revision for a job: validate, transcribe, analyse, render,
then stop at AWAITING_REVIEW. Every revision is immutable; a reprocess creates the
next one with the requested scope and reuses what the scope keeps."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from autopublisher import domain
from autopublisher.config import Settings
from autopublisher.domain import (
    ANALYZING,
    AWAITING_REVIEW,
    FAILED,
    FULL,
    GENERATING_ASSETS,
    INGESTED,
    METADATA_ONLY,
    REPROCESS_REQUESTED,
    SHORT,
    SHORTS_ONLY,
    THUMBNAIL,
    VALIDATING,
    Asset,
    AuditEvent,
    Job,
    Revision,
)
from autopublisher.jobstore import JobStore
from autopublisher.media import MediaError, MediaInfo, available_encoder, ffprobe
from autopublisher.objectstore import ObjectStore
from autopublisher.providers import ProviderError
from autopublisher.providers.analysis import (
    AnalysisInput,
    AnalysisValidationError,
    ContentAnalysisProvider,
    GroundingError,
    ground,
    validate_analysis,
)
from autopublisher.providers.transcription import (
    Transcript,
    TranscriptionProvider,
    TranscriptionRequest,
)
from autopublisher.render import CommandLog, render_short, render_thumbnail, verify_short
from autopublisher.shorts import ShortPlan, plan_shorts


class ProcessingFailed(RuntimeError):
    def __init__(self, job: Job, code: str, message: str):
        self.job, self.code, self.message = job, code, message
        super().__init__(f"{job.pk}: {code}: {message}")


@dataclass
class RenderOutput:
    shorts: list[Asset]
    thumbnails: list[Asset]
    log: CommandLog
    reports: dict


def work_prefix(job: Job, number: int) -> str:
    return f"work/{job.project_key}/{job.job_id}/rev-{number:04d}/"


def ready_prefix(job: Job, number: int) -> str:
    return f"ready/{job.project_key}/{job.job_id}/rev-{number:04d}/"


def render_plan(source: Path, info: MediaInfo, plans: list[ShortPlan], thumbnail_times: list[int],
                outdir: Path, encoder: str, log: CommandLog | None = None) -> RenderOutput:
    """Pure media step, usable in-process or inside the Fargate worker."""
    log = log or CommandLog()
    outdir.mkdir(parents=True, exist_ok=True)
    shorts, reports = [], {}
    for plan in plans:
        out = outdir / f"{plan.candidate_id}.mp4"
        result = render_short(source, info, plan, out, encoder, log)
        report = verify_short(out, plan, log)
        reports[plan.candidate_id] = report
        media = report["media"]
        shorts.append(Asset(
            asset_id=plan.candidate_id, type=SHORT, key=out.name, sha256=result["sha256"],
            duration_ms=media["duration_ms"], width=media["width"], height=media["height"],
            codec=media["video_codec"], size_bytes=result["size_bytes"], source_start_ms=plan.start_ms,
            source_end_ms=plan.end_ms, segment_ids=list(plan.segment_ids),
            quality={"ok": report["ok"], "problems": report["problems"], "loudness": report["loudness"],
                     "black_bars": report["black_bars"], "warnings": plan.warnings},
            metadata={"title_candidates": [plan.title_hint] if plan.title_hint else [],
                      "captions": [c.text for c in plan.captions]},
        ))
    thumbnails = []
    for i, at_ms in enumerate(thumbnail_times):
        out = outdir / f"thumb-{i + 1:02d}.jpg"
        result = render_thumbnail(source, at_ms, out, log)
        thumbnails.append(Asset(asset_id=f"thumb-{i + 1:02d}", type=THUMBNAIL, key=out.name, sha256=result["sha256"],
                                size_bytes=result["size_bytes"], source_start_ms=at_ms, source_end_ms=at_ms))
    return RenderOutput(shorts, thumbnails, log, reports)


class Processor:
    def __init__(self, settings: Settings, store: ObjectStore, jobs: JobStore,
                 transcription: TranscriptionProvider, analysis: ContentAnalysisProvider,
                 workdir: Path | None = None, encoder: str | None = None):
        self.settings, self.store, self.jobs = settings, store, jobs
        self.transcription, self.analysis = transcription, analysis
        self.workdir = workdir
        self._encoder = encoder

    @property
    def encoder(self) -> str:
        if self._encoder is None:
            self._encoder = available_encoder(self.settings.video_encoder)
        return self._encoder

    # -- persistence helpers ----------------------------------------------------------

    def _move(self, job: Job, new_state: str, actor: str, reason: str = "", **details) -> None:
        old = job.transition(new_state)
        self.jobs.update_job(job, expected_version=job.version)
        self.jobs.append_audit(AuditEvent(project_key=job.project_key, job_id=job.job_id, action="STATE",
                                          actor=actor, old_state=old, new_state=new_state,
                                          revision=job.current_revision, reason=reason, details=details))

    def _put_json(self, key: str, data) -> None:
        self.store.put_bytes(key, json.dumps(data, ensure_ascii=False, indent=2).encode(), "application/json")

    def _load_manifest(self, job: Job) -> dict | None:
        if not job.manifest_key:
            return None
        return json.loads(self.store.get_bytes(job.manifest_key, job.manifest_version_id))

    def _fail(self, job: Job, code: str, message: str, actor: str) -> ProcessingFailed:
        job.error = {"code": code, "message": message[:1000], "revision": job.current_revision}
        if job.can_transition(FAILED):
            self._move(job, FAILED, actor, reason=code, message=message[:300])
        else:
            self.jobs.update_job(job, expected_version=job.version)
        return ProcessingFailed(job, code, message)

    # -- main --------------------------------------------------------------------------

    def run(self, job: Job, scope: str = FULL, actor: str = "system:processor", reason: str = "") -> Revision:
        parent = self.jobs.get_revision(job.project_key, job.job_id, job.current_revision) \
            if job.current_revision else None
        number = job.current_revision + 1
        if job.state == INGESTED:
            scope = FULL
            self._move(job, VALIDATING, actor, revision=number)
        elif job.state == REPROCESS_REQUESTED:
            entry = domain.SCOPE_ENTRY_STATE[scope]
            self._move(job, entry, actor, reason=reason, scope=scope, revision=number)
        else:
            raise ProcessingFailed(job, "INVALID_STATE", f"cannot process a job in state {job.state}")

        revision = Revision(job_id=job.job_id, project_key=job.project_key, number=number, scope=scope,
                            parent_number=parent.number if parent else 0)
        prefix = work_prefix(job, number)
        try:
            with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
                tmpdir = Path(tmp)
                source = self.store.download(job.source_key, tmpdir / "source" / Path(job.source_key).name,
                                             job.source_version_id)
                info = ffprobe(source)
                manifest = self._load_manifest(job)
                if job.state == VALIDATING:
                    self._validate(job, info)
                    self._move(job, ANALYZING, actor, revision=number)

                if job.state == ANALYZING:
                    transcript = self._transcript(job, revision, parent, scope, source, manifest, info, prefix)
                    self._analyse(job, revision, parent, scope, transcript, manifest, info, prefix)
                    self._move(job, GENERATING_ASSETS, actor, revision=number)
                else:
                    transcript = self._reuse_transcript(revision, parent)
                    revision.analysis = dict(parent.analysis) if parent else {}
                    revision.analysis_key = parent.analysis_key if parent else ""
                    revision.analysis_provider = parent.analysis_provider if parent else ""
                    revision.prompt_version = parent.prompt_version if parent else ""
                    revision.master_metadata = dict(parent.master_metadata) if parent else {}

                self._assets(job, revision, parent, scope, source, info, manifest, transcript, tmpdir, prefix)
                revision.master = Asset(asset_id="master", type=domain.MASTER, key=job.source_key,
                                        version_id=job.source_version_id, sha256=job.source_sha256,
                                        duration_ms=info.duration_ms, width=info.width, height=info.height,
                                        codec=info.video_codec, size_bytes=info.size_bytes)
                revision.checksums = {"source": job.source_sha256,
                                      **{a.asset_id: a.sha256 for a in revision.shorts + revision.thumbnails}}
                self._put_json(prefix + "revision.json", revision.to_dict())
        except (ProviderError, MediaError, GroundingError, AnalysisValidationError) as exc:
            raise self._fail(job, type(exc).__name__.upper(), str(exc), actor) from exc

        self.jobs.put_revision(revision)
        job.current_revision = number
        job.reprocess_scope = ""
        job.error = {}
        self._move(job, AWAITING_REVIEW, actor, revision=number,
                   shorts=len(revision.shorts), warnings=len(revision.warnings))
        return revision

    # -- steps -----------------------------------------------------------------------------

    def _validate(self, job: Job, info: MediaInfo) -> None:
        if info.duration_ms <= 0 or info.width <= 0:
            raise MediaError("source has no decodable video")
        if job.source_duration_ms and abs(job.source_duration_ms - info.duration_ms) > 500:
            raise MediaError("source duration changed since ingestion")

    def _reuse_transcript(self, revision: Revision, parent: Revision | None) -> Transcript | None:
        if not parent or not parent.transcript_key:
            return None
        revision.transcript_key = parent.transcript_key
        revision.transcript_language = parent.transcript_language
        revision.transcript_provider = parent.transcript_provider
        revision.transcript_confidence = parent.transcript_confidence
        return Transcript.from_dict(json.loads(self.store.get_bytes(parent.transcript_key)))

    def _transcript(self, job: Job, revision: Revision, parent: Revision | None, scope: str, source: Path,
                    manifest: dict | None, info: MediaInfo, prefix: str) -> Transcript | None:
        if scope in (METADATA_ONLY, SHORTS_ONLY) and parent and parent.transcript_key:
            return self._reuse_transcript(revision, parent)
        if not info.has_audio:
            revision.warnings.append({"code": "NO_AUDIO", "message": "source has no audio track; transcript skipped"})
            return None
        request = TranscriptionRequest(source_path=source, source_uri=getattr(self.store, "uri", lambda k: "")(job.source_key),
                                       language_hint=job.language, manifest=manifest)
        transcript = self.transcription.transcribe(request)
        revision.transcript_key = prefix + "transcript.json"
        revision.transcript_language = transcript.language
        revision.transcript_provider = transcript.provider
        revision.transcript_confidence = transcript.mean_confidence
        self._put_json(revision.transcript_key, transcript.to_dict())
        low = transcript.low_confidence()
        if low:
            revision.warnings.append({"code": "LOW_CONFIDENCE_TRANSCRIPT",
                                      "message": f"{len(low)} segment(s) below confidence threshold",
                                      "segments": [{"start_ms": s.start_ms, "end_ms": s.end_ms} for s in low][:20]})
        if manifest and transcript.language and transcript.language != manifest["language"]:
            revision.warnings.append({"code": "LANGUAGE_MISMATCH",
                                      "message": f"transcript {transcript.language} vs manifest {manifest['language']}"})
        return transcript

    def _analyse(self, job: Job, revision: Revision, parent: Revision | None, scope: str,
                 transcript: Transcript | None, manifest: dict | None, info: MediaInfo, prefix: str) -> None:
        if manifest is None and not self.settings.allow_unstructured_analysis:
            raise GroundingError("no manifest and unstructured analysis is disabled")
        request = AnalysisInput(manifest=manifest, transcript=transcript, probe=info.as_dict(),
                                language=job.language, owner_notes=job.error.get("owner_notes", "") if job.error else "",
                                previous_analysis=parent.analysis if parent else None)
        raw = self.analysis.analyze(request)
        analysis = validate_analysis(raw)
        revision.warnings.extend(ground(analysis, manifest, transcript))
        revision.warnings.extend(analysis.get("warnings", []))
        revision.analysis = analysis
        revision.analysis_provider = self.analysis.name
        revision.prompt_version = self.analysis.prompt_version
        revision.analysis_key = prefix + "analysis.json"
        self._put_json(revision.analysis_key, analysis)
        usage = getattr(self.analysis, "last_usage", {}) or {}
        revision.cost = {"analysis_provider": self.analysis.name, "prompt_version": self.analysis.prompt_version,
                         "transcript_provider": revision.transcript_provider, "tokens": usage,
                         "note": "token counts only; monetary cost is computed from current provider prices in docs/cost-estimate.md"}
        youtube = manifest["youtube"] if manifest else {}
        revision.master_metadata = {
            "title_candidates": analysis["title_candidates"], "title": analysis["title_candidates"][0],
            "description": analysis["description"], "chapters": analysis["chapters"],
            "hashtags": analysis["hashtags"], "tags": analysis["tags"], "category": analysis["category"],
            "playlist": analysis["playlist"], "default_language": youtube.get("default_language", job.language),
            "made_for_kids": youtube.get("made_for_kids", False),
            "contains_synthetic_media": youtube.get("contains_synthetic_media", False),
            "notify_subscribers": youtube.get("notify_subscribers", False), "privacy": "private",
            "owner_confirmed_audience": False, "owner_confirmed_synthetic": False,
        }

    def _assets(self, job: Job, revision: Revision, parent: Revision | None, scope: str, source: Path,
                info: MediaInfo, manifest: dict | None, transcript: Transcript | None, tmpdir: Path,
                prefix: str) -> None:
        if scope == METADATA_ONLY and parent and parent.shorts:
            revision.shorts = [Asset.from_dict(a.__dict__ | {"review_status": domain.PENDING, "review_reason": ""})
                               for a in parent.shorts]
            revision.thumbnails = [Asset.from_dict(a.__dict__) for a in parent.thumbnails]
            revision.command_log_key = parent.command_log_key
            return
        plans, dropped = plan_shorts(manifest, revision.analysis, transcript, self.settings.max_shorts_per_job)
        revision.warnings.extend(dropped)
        for plan in plans:
            revision.warnings.extend({"candidate_id": plan.candidate_id, **w} for w in plan.warnings)
        thumb_times = [s["start_ms"] + 300 for s in (manifest or {}).get("segments", []) if s["type"] == "QUESTION"][:3]
        if not thumb_times:
            thumb_times = [max(0, info.duration_ms // 10)]
        self._put_json(prefix + "plan.json", {"shorts": [p.to_dict() for p in plans], "thumbnails": thumb_times})
        output = render_plan(source, info, plans, thumb_times, tmpdir / "out", self.encoder)
        ready = ready_prefix(job, revision.number)
        for asset in output.shorts + output.thumbnails:
            content_type = "video/mp4" if asset.type == SHORT else "image/jpeg"
            stored = self.store.upload_file(tmpdir / "out" / asset.key, ready + asset.key, content_type)
            asset.key, asset.version_id = stored.key, stored.version_id
            if asset.type == SHORT:
                self._short_metadata(asset, manifest, revision)
        for asset in output.shorts:
            if not asset.quality.get("ok", True):
                revision.warnings.append({"code": "SHORT_QUALITY", "candidate_id": asset.asset_id,
                                          "message": "; ".join(asset.quality.get("problems", []))})
        revision.shorts, revision.thumbnails = output.shorts, output.thumbnails
        revision.command_log_key = prefix + "commands.json"
        self.store.put_bytes(revision.command_log_key, output.log.to_json().encode(), "application/json")
        self._put_json(prefix + "render-report.json", output.reports)

    @staticmethod
    def _short_metadata(asset: Asset, manifest: dict | None, revision: Revision) -> None:
        master = revision.master_metadata
        titles = list(asset.metadata.get("title_candidates", []))
        captions = asset.metadata.get("captions", [])
        question = next((c for c in captions if c.endswith("?")), captions[0] if captions else "")
        for candidate in (question, master.get("title", "")):
            if candidate and candidate not in titles:
                titles.append(candidate[:100])
        while len(titles) < 3 and titles:
            titles.append(titles[-1])
        explanation = captions[-1] if captions else ""
        asset.metadata = {
            "title_candidates": titles[:3], "title": titles[0] if titles else "",
            "description": (explanation + ("\n\n" + master.get("description", "") if master.get("description") else ""))[:5000],
            "hashtags": list(master.get("hashtags", []))[:3], "tags": list(master.get("tags", []))[:15],
            "category": master.get("category", ""), "playlist": master.get("playlist", ""),
            "default_language": master.get("default_language", ""), "made_for_kids": master.get("made_for_kids", False),
            "contains_synthetic_media": master.get("contains_synthetic_media", False),
            "notify_subscribers": False, "privacy": "private", "captions": captions,
        }
