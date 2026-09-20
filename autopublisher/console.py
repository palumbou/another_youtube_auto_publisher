"""Review console: server-rendered HTML behind an authenticated session and an IP
allowlist. One `Console.handle(Request) -> Response` function serves the local
HTTP server and the Lambda adapter alike."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import re
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote

from autopublisher.config import Settings
from autopublisher.domain import (
    APPROVED,
    AWAITING_REVIEW,
    FAILED,
    SCHEDULED,
    SCOPES,
    Job,
    Revision,
)
from autopublisher.jobstore import ConflictError, JobStore
from autopublisher.objectstore import ObjectStore
from autopublisher.review import Actor, ReviewError, ReviewService, local_display

SESSION_COOKIE = "autopub_session"
SESSION_TTL = 12 * 3600
MEDIA_TTL = 600


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    form_lists: dict[str, list[str]] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    source_ip: str = ""
    claims: dict = field(default_factory=dict)  # verified upstream (API Gateway JWT authorizer)


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)

    @classmethod
    def html(cls, status: int, text: str, **kw) -> Response:
        return cls(status, text.encode("utf-8"), **kw)

    @classmethod
    def json(cls, status: int, data) -> Response:
        return cls(status, json.dumps(data, ensure_ascii=False, indent=2).encode(), "application/json")

    @classmethod
    def redirect(cls, location: str, cookies: list[str] | None = None) -> Response:
        return cls(303, b"", headers={"Location": location}, cookies=cookies or [])


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# --- Authentication ---------------------------------------------------------------------

class Authenticator(ABC):
    @abstractmethod
    def actor(self, request: Request) -> str | None: ...

    def login(self, request: Request) -> Response | None:  # pragma: no cover - default
        return None


class TokenSigner:
    def __init__(self, secret: bytes | None = None):
        self.secret = secret or secrets.token_bytes(32)

    def sign(self, payload: str, ttl: int) -> str:
        expires = int(time.time()) + ttl
        body = f"{payload}|{expires}"
        mac = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{body}|{mac}".encode()).decode().rstrip("=")

    def verify(self, token: str) -> str | None:
        try:
            padded = token + "=" * (-len(token) % 4)
            body, expires, mac = base64.urlsafe_b64decode(padded).decode().rsplit("|", 2)
        except (ValueError, UnicodeDecodeError):
            return None
        expected = hmac.new(self.secret, f"{body}|{expires}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected) or int(expires) < time.time():
            return None
        return body


class LocalAuthenticator(Authenticator):
    """Single seeded owner for LOCAL_MODE only. The password is generated at start
    and shown once by the runner; nothing is stored in the repository."""

    def __init__(self, settings: Settings, username: str = "owner", password: str | None = None):
        if not settings.local_mode:
            raise RuntimeError("LocalAuthenticator is only allowed in LOCAL_MODE")
        self.username = username
        self.password = password or secrets.token_urlsafe(12)
        self.signer = TokenSigner()
        self.failures = 0

    def actor(self, request: Request) -> str | None:
        token = request.cookies.get(SESSION_COOKIE, "")
        return self.signer.verify(token) if token else None

    def login(self, request: Request) -> Response | None:
        if request.method != "POST":
            return None
        user, password = request.form.get("username", ""), request.form.get("password", "")
        if hmac.compare_digest(user, self.username) and hmac.compare_digest(password, self.password):
            self.failures = 0
            token = self.signer.sign(self.username, SESSION_TTL)
            return Response.redirect("/", cookies=[f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"])
        self.failures += 1
        return Response.html(401, page("Sign in", "<p class='error'>Invalid credentials.</p>" + LOGIN_FORM))


class GatewayJwtAuthenticator(Authenticator):
    """API Gateway (HTTP API) validates the Cognito JWT before the Lambda runs and
    passes the claims; without verified claims the request is denied."""

    def __init__(self, allowed_subjects: tuple[str, ...] = ()):
        self.allowed_subjects = allowed_subjects

    def actor(self, request: Request) -> str | None:
        claims = request.claims or {}
        subject = claims.get("sub") or claims.get("cognito:username")
        if not subject:
            return None
        if self.allowed_subjects and subject not in self.allowed_subjects and claims.get("cognito:username") not in self.allowed_subjects:
            return None
        return claims.get("cognito:username") or claims.get("email") or subject


def ip_allowed(source_ip: str, allowlist: tuple[str, ...], local_mode: bool) -> bool:
    if not allowlist:
        # Fail closed outside local mode; local mode allows loopback only.
        return local_mode and source_ip in ("127.0.0.1", "::1", "localhost", "")
    try:
        addr = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


# --- HTML --------------------------------------------------------------------------------

CSS = """
body{font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:1rem;background:#f7f5f2;color:#222}
a{color:#1a5fb4} table{border-collapse:collapse;width:100%} td,th{padding:.4rem .6rem;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:.6rem;background:#555;color:#fff;font-size:.8rem;font-weight:600}
.card{background:#fff;border:1px solid #ddd;border-radius:.5rem;padding:1rem;margin:1rem 0}
video{max-width:100%;background:#000;border-radius:.4rem} .short video{max-width:260px}
.short{display:flex;gap:1rem;flex-wrap:wrap} .short>div:last-child{flex:1;min-width:18rem}
button{background:#1a5fb4;color:#fff;border:0;border-radius:.4rem;padding:.4rem .9rem;font-size:.95rem;cursor:pointer;margin:.2rem 0}
button.danger{background:#a51d2d} button.ok{background:#26a269}
textarea,input[type=text],input[type=datetime-local],select{width:100%;box-sizing:border-box;font:inherit;padding:.3rem}
.warn{background:#fff6e0;border:1px solid #e5a50a;border-radius:.4rem;padding:.4rem .8rem;margin:.3rem 0}
.error{background:#fdecec;border:1px solid #a51d2d;border-radius:.4rem;padding:.4rem .8rem}
.transcript span{cursor:pointer;margin-right:.3rem} .transcript .low{background:#ffe8a8}
small{color:#666} form.inline{display:inline}
"""

LOGIN_FORM = ("<form method='post' action='/login' class='card'><p><label>User<br><input type='text' name='username' autocomplete='username'></label></p>"
              "<p><label>Password<br><input type='password' name='password' autocomplete='current-password'></label></p>"
              "<button>Sign in</button></form>")


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><h1><a href='/'>Review console</a></h1>{body}"
            f"<p><small><a href='/logout'>Sign out</a></small></p></body></html>")


def badge(text: str) -> str:
    return f"<span class='badge'>{esc(text)}</span>"


def hidden(name: str, value) -> str:
    return f"<input type='hidden' name='{esc(name)}' value='{esc(value)}'>"


# --- Console ----------------------------------------------------------------------------------

JOB_PATH = re.compile(r"^/jobs/([a-z0-9][a-z0-9-]{1,62})/([a-z0-9][a-z0-9._-]{2,127})(/.*)?$")


class Console:
    def __init__(self, settings: Settings, jobs: JobStore, store: ObjectStore, review: ReviewService,
                 auth: Authenticator, on_reprocess: Callable[[Job, str, str], None] | None = None,
                 media_signer: TokenSigner | None = None):
        self.settings, self.jobs, self.store, self.review, self.auth = settings, jobs, store, review, auth
        self.on_reprocess = on_reprocess
        self.media_signer = media_signer or TokenSigner()

    # -- entry -------------------------------------------------------------------------------------

    def handle(self, request: Request) -> Response:
        if not ip_allowed(request.source_ip, self.settings.ip_allowlist, self.settings.local_mode):
            return Response.html(403, page("Denied", "<p class='error'>Your address is not on the allowlist.</p>"))
        if request.path == "/healthz":
            return Response.json(200, {"ok": True})
        if request.path == "/login":
            if request.method == "POST":
                return self.auth.login(request) or Response.html(405, page("Sign in", LOGIN_FORM))
            return Response.html(200, page("Sign in", LOGIN_FORM))
        if request.path == "/logout":
            return Response.redirect("/login", cookies=[f"{SESSION_COOKIE}=; Path=/; Max-Age=0"])
        if request.path.startswith("/media/"):
            return self._media(request)
        actor_name = self.auth.actor(request)
        if not actor_name:
            return Response.redirect("/login") if request.method == "GET" else Response.html(401, page("Sign in", LOGIN_FORM))
        actor = Actor(actor_name, request.source_ip)
        try:
            return self._route(request, actor)
        except ReviewError as exc:
            return Response.html(400, page("Cannot do that", f"<p class='error'>{esc(exc)}</p><p><a href='javascript:history.back()'>Back</a></p>"))
        except ConflictError as exc:
            return Response.html(409, page("Conflict", f"<p class='error'>{esc(exc)}</p><p>Reload the job and try again.</p>"))

    def _route(self, request: Request, actor: Actor) -> Response:
        if request.path == "/" and request.method == "GET":
            return Response.html(200, self.render_index())
        if request.path.startswith("/api/jobs/"):
            match = JOB_PATH.match(request.path[len("/api"):])
            if match and request.method == "GET":
                return Response.json(200, self.review.read_model(match.group(1), match.group(2)))
        match = JOB_PATH.match(request.path)
        if not match:
            return Response.html(404, page("Not found", "<p>No such page.</p>"))
        project_key, job_id, action = match.group(1), match.group(2), (match.group(3) or "").strip("/")
        job = self.jobs.get_job(project_key, job_id)
        if job is None:
            return Response.html(404, page("Not found", "<p>No such job.</p>"))
        if request.method == "GET" and not action:
            return Response.html(200, self.render_job(job))
        if request.method == "GET" and action == "audit":
            return Response.json(200, [e.to_dict() for e in self.jobs.list_audit(project_key, job_id)])
        if request.method != "POST":
            return Response.html(405, page("Method not allowed", "<p>Wrong method.</p>"))
        return self._action(request, actor, job, action)

    # -- actions ------------------------------------------------------------------------------------

    def _action(self, request: Request, actor: Actor, job: Job, action: str) -> Response:
        form = request.form
        version = int(form.get("version") or job.version)
        pk, jid = job.project_key, job.job_id
        back = f"/jobs/{pk}/{jid}"
        if action.startswith("metadata/"):
            asset_id = action.split("/", 1)[1]
            changes = self._metadata_changes(form, request.form_lists)
            self.review.update_metadata(pk, jid, asset_id, changes, actor, expected_version=version)
        elif action == "approve-master":
            self.review.approve_master(pk, jid, actor, expected_version=version)
        elif action.startswith("shorts/") and action.endswith("/approve"):
            self.review.review_short(pk, jid, action.split("/")[1], True, actor, expected_version=version)
        elif action.startswith("shorts/") and action.endswith("/reject"):
            self.review.review_short(pk, jid, action.split("/")[1], False, actor, reason=form.get("reason", ""),
                                     expected_version=version)
        elif action == "approve-batch":
            self.review.approve_selected(pk, jid, actor, include_master=form.get("include_master") == "on",
                                         short_ids=request.form_lists.get("short_ids", []), expected_version=version)
        elif action == "reject":
            scope = form.get("scope", "")
            self.review.reject(pk, jid, form.get("reason", ""), scope, actor, expected_version=version)
            if self.on_reprocess:
                self.on_reprocess(self.jobs.get_job(pk, jid), scope, form.get("reason", ""))
        elif action == "cancel":
            self.review.cancel(pk, jid, form.get("reason", ""), actor, expected_version=version)
        elif action == "schedule":
            self.review.schedule(pk, jid, form.get("asset_id", ""), form.get("publish_at", ""), actor,
                                 notify_subscribers=form.get("notify_subscribers") == "on", expected_version=version)
        else:
            return Response.html(404, page("Not found", "<p>No such action.</p>"))
        return Response.redirect(back)

    @staticmethod
    def _metadata_changes(form: dict[str, str], lists: dict[str, list[str]]) -> dict:
        changes: dict = {}
        for key in ("title", "description", "category", "playlist", "default_language"):
            if key in form:
                changes[key] = form[key].strip()
        if "hashtags" in form:
            changes["hashtags"] = [h.strip() for h in re.split(r"[\s,]+", form["hashtags"]) if h.strip()]
        if "tags" in form:
            changes["tags"] = [t.strip() for t in form["tags"].split(",") if t.strip()]
        if "chapters" in form:
            chapters = []
            for line in form["chapters"].splitlines():
                m = re.match(r"^\s*(\d+):(\d\d)\s+(.+)$", line)
                if m:
                    chapters.append({"start_ms": (int(m.group(1)) * 60 + int(m.group(2))) * 1000, "title": m.group(3).strip()})
            changes["chapters"] = chapters
        for flag in ("made_for_kids", "contains_synthetic_media", "notify_subscribers",
                     "owner_confirmed_audience", "owner_confirmed_synthetic"):
            if flag in lists or f"{flag}_present" in form:
                changes[flag] = form.get(flag) == "on"
        return changes

    # -- media ----------------------------------------------------------------------------------------

    def media_url(self, key: str) -> str:
        if not self.settings.local_mode:
            return self.store.signed_url(key, MEDIA_TTL)
        token = self.media_signer.sign(key, MEDIA_TTL)
        return f"/media/{quote(key)}?t={token}"

    def _media(self, request: Request) -> Response:
        key = request.path[len("/media/"):]
        payload = self.media_signer.verify(request.query.get("t", ""))
        if not self.settings.local_mode or payload != key:
            return Response.html(403, page("Denied", "<p>Expired or invalid media link.</p>"))
        info = self.store.head(key)
        if info is None:
            return Response.html(404, page("Not found", "<p>No such object.</p>"))
        content_type = "video/mp4" if key.endswith(".mp4") else "image/jpeg" if key.endswith((".jpg", ".jpeg")) else "application/octet-stream"
        return Response(200, self.store.get_bytes(key), content_type, headers={"Cache-Control": "private, max-age=60"})

    # -- views ----------------------------------------------------------------------------------------

    def render_index(self) -> str:
        jobs = self.jobs.list_jobs()
        if not jobs:
            return page("Jobs", "<p>No jobs yet. A job appears when a READY marker is ingested after the cutover.</p>")
        rows = []
        for j in jobs:
            schedules = [s for s in self.jobs.list_schedules() if s.job_id == j.job_id and s.project_key == j.project_key]
            planned = ", ".join(local_display(s.publish_at_utc, s.owner_timezone) for s in schedules) or "—"
            revision = self.jobs.get_revision(j.project_key, j.job_id, j.current_revision) if j.current_revision else None
            warnings = len(revision.warnings) if revision else 0
            rows.append(f"<tr><td>{esc(j.project_key)}</td><td><a href='/jobs/{esc(j.project_key)}/{esc(j.job_id)}'>{esc(j.job_id)}</a></td>"
                        f"<td>{badge(j.state)}</td><td>{j.current_revision}</td><td>{j.source_duration_ms / 1000:.1f}s</td>"
                        f"<td>{esc(j.created_at)}</td><td>{warnings}</td><td>{esc(planned)}</td>"
                        f"<td>{esc(revision.master_review_status if revision else '—')}</td></tr>")
        table = ("<table><tr><th>Project</th><th>Job</th><th>State</th><th>Rev</th><th>Duration</th><th>Created (UTC)</th>"
                 "<th>Warnings</th><th>Planned (" + esc(self.settings.owner_timezone) + ")</th><th>Master review</th></tr>" + "".join(rows) + "</table>")
        kill = "<p class='warn'>Publish kill switch is ON: nothing is uploaded.</p>" if self.settings.publish_kill_switch else ""
        return page("Jobs", kill + table)

    def _metadata_form(self, job: Job, asset_id: str, meta: dict) -> str:
        chapters = "\n".join(f"{c['start_ms'] // 60000}:{(c['start_ms'] // 1000) % 60:02d} {c['title']}" for c in meta.get("chapters", []))
        titles = "".join(f"<li>{esc(t)}</li>" for t in meta.get("title_candidates", []))
        flags = ""
        for flag, label in (("made_for_kids", "Made for kids"), ("contains_synthetic_media", "Contains synthetic media"),
                            ("notify_subscribers", "Notify subscribers"), ("owner_confirmed_audience", "I confirm the audience decision"),
                            ("owner_confirmed_synthetic", "I confirm the synthetic-media declaration")):
            checked = "checked" if meta.get(flag) else ""
            flags += f"<label><input type='checkbox' name='{flag}' {checked}>{hidden(flag + '_present', '1')} {label}</label><br>"
        return (f"<form method='post' action='/jobs/{esc(job.project_key)}/{esc(job.job_id)}/metadata/{esc(asset_id)}'>{hidden('version', job.version)}"
                f"<p>Title candidates:</p><ol>{titles}</ol>"
                f"<p><label>Title<br><input type='text' name='title' value='{esc(meta.get('title', ''))}' maxlength='100'></label></p>"
                f"<p><label>Description<br><textarea name='description' rows='6'>{esc(meta.get('description', ''))}</textarea></label></p>"
                f"<p><label>Chapters (mm:ss title per line)<br><textarea name='chapters' rows='3'>{esc(chapters)}</textarea></label></p>"
                f"<p><label>Hashtags (1–3)<br><input type='text' name='hashtags' value='{esc(' '.join(meta.get('hashtags', [])))}'></label></p>"
                f"<p><label>Tags (comma separated, few)<br><input type='text' name='tags' value='{esc(', '.join(meta.get('tags', [])))}'></label></p>"
                f"<p><label>Category<br><input type='text' name='category' value='{esc(meta.get('category', ''))}'></label></p>"
                f"<p><label>Playlist<br><input type='text' name='playlist' value='{esc(meta.get('playlist', ''))}'></label></p>"
                f"<p><label>Language<br><input type='text' name='default_language' value='{esc(meta.get('default_language', ''))}'></label></p>"
                f"<p>{flags}</p><p>Privacy at upload: <strong>private</strong> (always).</p><button>Save metadata</button></form>")

    def render_job(self, job: Job) -> str:
        pk, jid = esc(job.project_key), esc(job.job_id)
        base = f"/jobs/{pk}/{jid}"
        revision = self.jobs.get_revision(job.project_key, job.job_id, job.current_revision) if job.current_revision else None
        parts = [f"<h2>{jid} {badge(job.state)} <small>revision {job.current_revision} · version {job.version}</small></h2>",
                 f"<p><small>{esc(job.source_key)} · {job.source_duration_ms / 1000:.1f}s · ingested {esc(job.created_at)} · {esc(job.cutover_decision)}</small></p>"]
        if job.error:
            parts.append(f"<div class='error'>{esc(json.dumps(job.error, ensure_ascii=False))}</div>")
        if revision is None:
            parts.append("<p>Processing has not produced a revision yet.</p>")
            return page(job.job_id, "".join(parts))
        for w in revision.warnings:
            parts.append(f"<div class='warn'><strong>{esc(w.get('code', ''))}</strong> {esc(w.get('message', ''))} {esc(w.get('candidate_id', ''))}</div>")
        # master
        master_url = self.media_url(job.source_key)
        parts.append(f"<div class='card'><h3>Master {badge(revision.master_review_status)}</h3>"
                     f"<video controls preload='metadata' src='{esc(master_url)}'></video>"
                     f"{self._transcript_html(revision)}{self._metadata_form(job, 'master', revision.master_metadata)}")
        if job.state == AWAITING_REVIEW:
            parts.append(f"<form method='post' action='{base}/approve-master' class='inline'>{hidden('version', job.version)}<button class='ok'>Approve master</button></form>")
        parts.append("</div>")
        # shorts
        for asset in revision.shorts:
            url = self.media_url(asset.key)
            quality = "OK" if asset.quality.get("ok", True) else "; ".join(asset.quality.get("problems", []))
            loud = asset.quality.get("loudness", {})
            parts.append(f"<div class='card short'><div><video controls preload='metadata' src='{esc(url)}'></video></div><div>"
                         f"<h3>{esc(asset.asset_id)} {badge(asset.review_status)}</h3>"
                         f"<p><small>source {asset.source_start_ms / 1000:.1f}s → {asset.source_end_ms / 1000:.1f}s · {asset.duration_ms / 1000:.1f}s · "
                         f"{asset.width}x{asset.height} {esc(asset.codec)} · loudness {esc(loud.get('integrated_lufs'))} LUFS · quality: {esc(quality)}</small></p>"
                         f"<p><small>segments: {esc(', '.join(asset.segment_ids))}</small></p>"
                         f"{self._metadata_form(job, asset.asset_id, asset.metadata)}")
            if job.state == AWAITING_REVIEW:
                parts.append(f"<form method='post' action='{base}/shorts/{esc(asset.asset_id)}/approve' class='inline'>{hidden('version', job.version)}<button class='ok'>Approve short</button></form> "
                             f"<form method='post' action='{base}/shorts/{esc(asset.asset_id)}/reject' class='inline'>{hidden('version', job.version)}"
                             f"<input type='text' name='reason' placeholder='reason (required)'><button class='danger'>Reject short</button></form>")
            if asset.review_reason:
                parts.append(f"<p class='warn'>Rejected: {esc(asset.review_reason)}</p>")
            parts.append("</div></div>")
        # thumbnails
        if revision.thumbnails:
            thumbs = "".join(f"<img src='{esc(self.media_url(t.key))}' alt='{esc(t.asset_id)}' style='max-width:200px;margin:.3rem'>" for t in revision.thumbnails)
            parts.append(f"<div class='card'><h3>Thumbnail candidates</h3>{thumbs}<p><small>{esc('; '.join(revision.analysis.get('thumbnail_concepts', [])))}</small></p></div>")
        # previous revision diff
        if revision.parent_number:
            parent = self.jobs.get_revision(job.project_key, job.job_id, revision.parent_number)
            if parent:
                parts.append(f"<div class='card'><h3>Compared with revision {parent.number} ({esc(parent.scope)} → {esc(revision.scope)})</h3>"
                             f"<p>Previous title: {esc(parent.master_metadata.get('title', ''))}<br>Current title: {esc(revision.master_metadata.get('title', ''))}</p>"
                             f"<p>Previous shorts: {esc(', '.join(a.asset_id + '@' + a.sha256[:8] for a in parent.shorts))}<br>"
                             f"Current shorts: {esc(', '.join(a.asset_id + '@' + a.sha256[:8] for a in revision.shorts))}</p>"
                             f"<p>Rejection reason: {esc(parent.reject_reason)}</p></div>")
        # batch / reject / cancel / schedule
        if job.state == AWAITING_REVIEW:
            boxes = "".join(f"<label><input type='checkbox' name='short_ids' value='{esc(a.asset_id)}'> {esc(a.asset_id)}</label><br>" for a in revision.shorts)
            scopes = "".join(f"<option value='{s}'>{s}</option>" for s in SCOPES)
            parts.append(f"<div class='card'><h3>Decision</h3>"
                         f"<form method='post' action='{base}/approve-batch'>{hidden('version', job.version)}"
                         f"<label><input type='checkbox' name='include_master' checked> master</label><br>{boxes}<button class='ok'>Approve selected</button></form><hr>"
                         f"<form method='post' action='{base}/reject'>{hidden('version', job.version)}<p><label>Reason (required)<br><textarea name='reason' rows='2'></textarea></label></p>"
                         f"<p><label>Reprocess scope <select name='scope'>{scopes}</select></label></p><button class='danger'>Reject and reprocess</button></form><hr>"
                         f"<form method='post' action='{base}/cancel'>{hidden('version', job.version)}<input type='text' name='reason' placeholder='reason'><button class='danger'>Cancel job</button></form></div>")
        if job.state == FAILED:
            scopes = "".join(f"<option value='{s}'>{s}</option>" for s in SCOPES)
            parts.append(f"<div class='card'><h3>Redrive</h3><form method='post' action='{base}/reject'>{hidden('version', job.version)}"
                         f"<input type='text' name='reason' placeholder='reason (required)'> <select name='scope'>{scopes}</select><button>Reprocess</button></form></div>")
        if job.state in (APPROVED, SCHEDULED):
            options = "".join(f"<option value='{esc(a.asset_id)}'>{esc(a.asset_id)}</option>" for a in revision.approved_assets())
            existing = [s for s in self.jobs.list_schedules() if s.job_id == job.job_id and s.project_key == job.project_key]
            rows = "".join(f"<tr><td>{esc(s.asset_id)}</td><td>{esc(local_display(s.publish_at_utc, s.owner_timezone))}</td><td>{esc(s.publish_at_utc)}</td><td>{badge(s.status)}</td></tr>" for s in existing)
            parts.append(f"<div class='card'><h3>Schedule (times in {esc(self.settings.owner_timezone)}, stored in UTC)</h3>"
                         f"<table><tr><th>Asset</th><th>Local</th><th>UTC</th><th>Status</th></tr>{rows}</table>"
                         f"<form method='post' action='{base}/schedule'>{hidden('version', job.version)}<select name='asset_id'>{options}</select> "
                         f"<input type='datetime-local' name='publish_at'> <label><input type='checkbox' name='notify_subscribers'> notify subscribers</label>"
                         f"<button>Schedule</button></form>"
                         f"<form method='post' action='{base}/cancel'>{hidden('version', job.version)}<input type='text' name='reason' placeholder='reason'><button class='danger'>Cancel job</button></form></div>")
        parts.append(f"<p><a href='{base}/audit'>Audit log (JSON)</a> · <a href='/api{base}'>Read model (JSON)</a></p>")
        return page(job.job_id, "".join(parts))

    def _transcript_html(self, revision: Revision) -> str:
        if not revision.transcript_key:
            return "<p><small>No transcript.</small></p>"
        try:
            data = json.loads(self.store.get_bytes(revision.transcript_key))
        except FileNotFoundError:
            return "<p><small>Transcript missing.</small></p>"
        spans = "".join(
            f"<span class='{'low' if s['confidence'] < 0.75 else ''}' title='{s['confidence']:.2f}' "
            f"onclick=\"this.closest('.card').querySelector('video').currentTime={s['start_ms'] / 1000:.2f}\">{esc(s['text'])}</span>"
            for s in data.get("segments", []))
        return (f"<p><small>Transcript ({esc(data.get('language', ''))}, {esc(data.get('provider', ''))}, mean confidence "
                f"{revision.transcript_confidence:.2f}; highlighted = low confidence; click to seek)</small></p><p class='transcript'>{spans}</p>")


# --- Adapters ---------------------------------------------------------------------------------------

def request_from_lambda(event: dict) -> Request:
    http = event.get("requestContext", {}).get("http", {})
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    parsed = parse_qs(body, keep_blank_values=True)
    cookies = {}
    for raw in event.get("cookies") or []:
        name, _, value = raw.partition("=")
        cookies[name] = value
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return Request(method=http.get("method", "GET"), path=event.get("rawPath") or "/",
                   query=event.get("queryStringParameters") or {}, form={k: v[0] for k, v in parsed.items()},
                   form_lists=parsed, cookies=cookies, headers=event.get("headers") or {},
                   source_ip=http.get("sourceIp", ""), claims=claims)


def response_to_lambda(response: Response) -> dict:
    is_text = response.content_type.startswith(("text/", "application/json"))
    return {"statusCode": response.status,
            "headers": {"Content-Type": response.content_type, **response.headers},
            "cookies": response.cookies,
            "isBase64Encoded": not is_text,
            "body": response.body.decode() if is_text else base64.b64encode(response.body).decode()}
