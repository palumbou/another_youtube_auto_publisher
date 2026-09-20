"""Local development runner: filesystem bucket, JSON state, mock providers, fake
YouTube adapter, seeded owner login. No real publishing is possible from here.

  python -m autopublisher.local serve   --bucket-root DIR --state-root DIR [--port 8080]
  python -m autopublisher.local ingest  --bucket-root DIR --state-root DIR      # scan READY markers, ingest, process
  python -m autopublisher.local status  --state-root DIR [--job ID]
  python -m autopublisher.local confirm --state-root DIR --job ID [--asset master] ...
  python -m autopublisher.local approve --state-root DIR --job ID [--master] [--short ID ...]
  python -m autopublisher.local reject  --bucket-root DIR --state-root DIR --job ID --reason TEXT --scope SCOPE
  python -m autopublisher.local schedule --state-root DIR --job ID --asset ID --at "2026-10-01T10:00"
  python -m autopublisher.local publish --bucket-root DIR --state-root DIR [--now ISO]  # fake adapter only
  python -m autopublisher.local e2e     --workdir DIR                              # full flow on a generated fixture
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from autopublisher import domain
from autopublisher.config import Settings
from autopublisher.console import Console, LocalAuthenticator, Request
from autopublisher.ingest import CREATED, Ingestor, ReadyEvent
from autopublisher.jobstore import LocalJobStore
from autopublisher.objectstore import FilesystemObjectStore
from autopublisher.processing import ProcessingFailed, Processor
from autopublisher.providers.analysis import ManifestAnalysisProvider
from autopublisher.providers.transcription import ManifestTranscriptionProvider
from autopublisher.publishing import FakeYouTubeAdapter, Publisher
from autopublisher.review import Actor, ReviewService

CLI_ACTOR = Actor("owner:cli", "127.0.0.1")


def local_settings(env: dict | None = None) -> Settings:
    base = {"ENVIRONMENT": "local", "LOCAL_MODE": "1", "CUTOVER_AT": "2026-09-20T00:00:00Z",
            "PUBLISH_KILL_SWITCH": "0", "YOUTUBE_MODE": "fake"}
    base.update({k: v for k, v in os.environ.items() if k in ("CUTOVER_AT", "ALLOWED_PROJECTS", "OWNER_TIMEZONE",
                                                                "PUBLISH_KILL_SWITCH", "MAX_SHORTS_PER_JOB",
                                                                "YOUTUBE_API_PROJECT_AUDITED", "VIDEO_ENCODER")})
    base.update(env or {})
    return Settings.from_env(base)


class LocalStack:
    def __init__(self, bucket_root: Path, state_root: Path, settings: Settings | None = None):
        self.settings = settings or local_settings()
        self.store = FilesystemObjectStore(bucket_root)
        self.jobs = LocalJobStore(state_root)
        self.workdir = state_root / "tmp"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.ingestor = Ingestor(self.settings, self.store, self.jobs, workdir=self.workdir)
        self.processor = Processor(self.settings, self.store, self.jobs, ManifestTranscriptionProvider(),
                                   ManifestAnalysisProvider(), workdir=self.workdir)
        self.review = ReviewService(self.settings, self.jobs)
        self.adapter = FakeYouTubeAdapter()
        self.publisher = Publisher(self.settings, self.store, self.jobs, self.adapter, workdir=self.workdir)

    # -- queue emulation ------------------------------------------------------------------

    def scan_ready(self) -> list[dict]:
        """Emulate S3 events: one event per READY marker under the incoming prefix."""
        results = []
        for info in self.store.list(self.settings.incoming_prefix):
            if info.key.endswith("/READY"):
                result = self.ingestor.handle_ready(ReadyEvent(bucket="local", key=info.key,
                                                               version_id=info.version_id, event_time=info.last_modified))
                results.append(result.as_dict())
                if result.status == CREATED:
                    self.process(result.job)
        return results

    def process(self, job: domain.Job, scope: str = domain.FULL, reason: str = "") -> dict:
        try:
            revision = self.processor.run(job, scope=scope, reason=reason)
            return {"job": job.pk, "revision": revision.number, "state": job.state, "shorts": len(revision.shorts)}
        except ProcessingFailed as exc:
            return {"job": job.pk, "state": exc.job.state, "error": exc.code, "message": exc.message}

    def process_pending(self) -> list[dict]:
        out = []
        for job in self.jobs.list_jobs():
            if job.state == domain.REPROCESS_REQUESTED:
                out.append(self.process(job, job.reprocess_scope or domain.FULL, job.error.get("owner_notes", "")))
        return out


# --- HTTP server --------------------------------------------------------------------------

def serve(stack: LocalStack, port: int, poll_seconds: float = 3.0) -> None:
    auth = LocalAuthenticator(stack.settings)
    app = Console(stack.settings, stack.jobs, stack.store, stack.review, auth,
                  on_reprocess=lambda job, scope, reason: None)  # the poller picks REPROCESS_REQUESTED up
    print(f"Local console: http://127.0.0.1:{port}/  user: {auth.username}  password: {auth.password}", flush=True)
    print("(the password is generated per run and never stored)", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self):
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            parsed = parse_qs(body, keep_blank_values=True)
            cookies = {}
            for raw in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = raw.strip().partition("=")
                if name:
                    cookies[name] = value
            request = Request(method=self.command, path=unquote(parts.path),
                              query={k: v[0] for k, v in parse_qs(parts.query).items()},
                              form={k: v[0] for k, v in parsed.items()}, form_lists=parsed, cookies=cookies,
                              headers=dict(self.headers), source_ip=self.client_address[0])
            response = app.handle(request)
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for k, v in response.headers.items():
                self.send_header(k, v)
            for cookie in response.cookies:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = do_POST = _dispatch

        def log_message(self, fmt, *args):  # quieter, no cookies in logs
            sys.stderr.write(f"{self.client_address[0]} {self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

    stop = threading.Event()

    def poller():
        while not stop.is_set():
            try:
                for result in stack.scan_ready():
                    if result["status"] != "DUPLICATE":
                        print("ingest:", json.dumps(result), flush=True)
                for result in stack.process_pending():
                    print("reprocess:", json.dumps(result), flush=True)
                for outcome in stack.publisher.run_due():
                    if outcome.status != "BLOCKED":
                        print("publish:", json.dumps(outcome.__dict__), flush=True)
            except Exception as exc:  # noqa: BLE001 - keep the local loop alive, report the error
                print("poller error:", type(exc).__name__, exc, flush=True)
            stop.wait(poll_seconds)

    thread = threading.Thread(target=poller, daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


# --- fixture generation ---------------------------------------------------------------------

def generate_fixture_job(bucket_root: Path, job_id: str, seconds: int = 42) -> Path:
    """Render a synthetic quiz master with ffmpeg and a matching manifest, then write READY last."""
    import hashlib
    import subprocess

    from autopublisher.contract import CONTRACT_DIR
    from autopublisher.media import available_encoder, ffprobe

    prefix = bucket_root / "incoming" / "quiz-al-volo" / job_id
    (prefix / "source").mkdir(parents=True, exist_ok=True)
    (prefix / "manifest").mkdir(parents=True, exist_ok=True)
    master = prefix / "source" / "master.mp4"
    encoder = available_encoder()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           "testsrc2=size=1920x1080:rate=30", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
           "-t", str(seconds), "-c:v", encoder]
    cmd += ["-preset", "veryfast", "-crf", "23"] if encoder == "libx264" else ["-b:v", "4M"]
    cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(master)]
    subprocess.run(cmd, check=True)
    info = ffprobe(master)
    manifest = json.loads((CONTRACT_DIR / "1.0.0" / "fixtures" / "valid.json").read_text())
    manifest["job_id"] = job_id
    manifest["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest["source"].update({"sha256": hashlib.sha256(master.read_bytes()).hexdigest(), "duration_ms": info.duration_ms,
                               "width": info.width, "height": info.height, "frame_rate": info.frame_rate})
    manifest["producer"]["git_commit"] = "c" * 40
    manifest["rights"]["notes"] = "Synthetic e2e fixture generated locally; not for publication."
    (prefix / "manifest" / "video-job-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (prefix / "READY").write_bytes(b"")
    return prefix


def run_e2e(workdir: Path) -> dict:
    """The acceptance flow on a generated fixture: ingest twice (duplicate), reject
    a revision with a reason, reprocess only that scope, approve, schedule, fake upload."""
    bucket, state = workdir / "bucket", workdir / "state"
    stack = LocalStack(bucket, state)
    job_id = f"e2e-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    generate_fixture_job(bucket, job_id)
    report: dict = {"job_id": job_id, "steps": []}
    first = stack.scan_ready()
    second = stack.scan_ready()
    report["steps"].append({"ingest": first, "duplicate_ingest": second})
    job = stack.jobs.get_job("quiz-al-volo", job_id)
    assert job.state == domain.AWAITING_REVIEW, job.state
    assert second[0]["status"] == "DUPLICATE"
    stack.review.reject("quiz-al-volo", job_id, "title too generic for the pilot", domain.METADATA_ONLY, CLI_ACTOR)
    report["steps"].append({"reject": stack.process_pending()})
    job = stack.jobs.get_job("quiz-al-volo", job_id)
    assert job.current_revision == 2 and job.state == domain.AWAITING_REVIEW
    for asset in ("master", "short-01"):
        stack.review.update_metadata("quiz-al-volo", job_id, asset, {"owner_confirmed_audience": True}, CLI_ACTOR)
    stack.review.approve_selected("quiz-al-volo", job_id, CLI_ACTOR, include_master=True, short_ids=["short-01"])
    when = datetime.now(UTC).replace(microsecond=0)
    for asset in ("master", "short-01"):
        stack.review.schedule("quiz-al-volo", job_id, asset, (when.replace(year=when.year + 1)).isoformat(), CLI_ACTOR)
    outcomes = stack.publisher.run_due(when.replace(year=when.year + 1))
    report["steps"].append({"publish": [o.__dict__ for o in outcomes]})
    job = stack.jobs.get_job("quiz-al-volo", job_id)
    report["final_state"] = job.state
    report["publications"] = [domain.scrub(p.to_dict()) for p in stack.jobs.list_publications()]
    report["fake_requests"] = [r["op"] for r in stack.adapter.requests]
    report["audit_actions"] = [e.action for e in stack.jobs.list_audit("quiz-al-volo", job_id)]
    revisions = stack.jobs.list_revisions("quiz-al-volo", job_id)
    report["revisions"] = [{"number": r.number, "scope": r.scope, "shorts": [(a.asset_id, a.sha256[:12], a.quality.get("ok")) for a in r.shorts],
                            "warnings": [w.get("code") for w in r.warnings]} for r in revisions]
    assert job.state == domain.UPLOADED_PRIVATE
    assert report["fake_requests"].count("upload") == 2
    assert len({p["youtube_video_id"] for p in report["publications"]}) == 2
    return report


# --- CLI ---------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autopublisher.local", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, bucket=True):
        if bucket:
            p.add_argument("--bucket-root", required=True, type=Path)
        p.add_argument("--state-root", required=True, type=Path)

    p = sub.add_parser("serve"); common(p); p.add_argument("--port", type=int, default=8080)
    p = sub.add_parser("ingest"); common(p)
    p = sub.add_parser("status"); common(p, bucket=False); p.add_argument("--job")
    p = sub.add_parser("confirm"); common(p, bucket=False); p.add_argument("--job", required=True); p.add_argument("--asset", default="master")
    p.add_argument("--synthetic", action="store_true")
    p = sub.add_parser("approve"); common(p, bucket=False); p.add_argument("--job", required=True); p.add_argument("--master", action="store_true")
    p.add_argument("--short", action="append", default=[])
    p = sub.add_parser("reject"); common(p); p.add_argument("--job", required=True); p.add_argument("--reason", required=True)
    p.add_argument("--scope", required=True, choices=domain.SCOPES)
    p = sub.add_parser("schedule"); common(p, bucket=False); p.add_argument("--job", required=True); p.add_argument("--asset", required=True)
    p.add_argument("--at", required=True); p.add_argument("--notify", action="store_true")
    p = sub.add_parser("publish"); common(p); p.add_argument("--now")
    p = sub.add_parser("e2e"); p.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args(argv)

    project = os.environ.get("PROJECT_KEY", "quiz-al-volo")
    if args.command == "e2e":
        args.workdir.mkdir(parents=True, exist_ok=True)
        report = run_e2e(args.workdir)
        (args.workdir / "e2e-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.command == "serve":
        serve(LocalStack(args.bucket_root, args.state_root), args.port)
        return 0
    stack = LocalStack(getattr(args, "bucket_root", None) or args.state_root / "unused-bucket", args.state_root)
    if args.command == "ingest":
        out = stack.scan_ready() + stack.process_pending()
    elif args.command == "status":
        if args.job:
            out = stack.review.read_model(project, args.job) | {"audit": [e.to_dict() for e in stack.jobs.list_audit(project, args.job)]}
        else:
            out = [{"job_id": j.job_id, "state": j.state, "revision": j.current_revision} for j in stack.jobs.list_jobs()]
    elif args.command == "confirm":
        changes = {"owner_confirmed_audience": True}
        if args.synthetic:
            changes["owner_confirmed_synthetic"] = True
        out = stack.review.update_metadata(project, args.job, args.asset, changes, CLI_ACTOR).master_metadata if args.asset == "master" else "ok"
    elif args.command == "approve":
        out = stack.review.approve_selected(project, args.job, CLI_ACTOR, include_master=args.master, short_ids=args.short).state
    elif args.command == "reject":
        stack.review.reject(project, args.job, args.reason, args.scope, CLI_ACTOR)
        out = stack.process_pending()
    elif args.command == "schedule":
        out = stack.review.schedule(project, args.job, args.asset, args.at, CLI_ACTOR, notify_subscribers=args.notify).to_dict()
    elif args.command == "publish":
        now = datetime.fromisoformat(args.now).astimezone(UTC) if args.now else None
        out = [o.__dict__ for o in stack.publisher.run_due(now)]
    else:
        parser.error("unknown command")
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

