"""Review web UI: one Lambda behind a Function URL, server-rendered HTML, stdlib only.

Routes:
  GET  /                    jobs list
  GET  /job/<id>            job detail: Short previews (presigned S3), metadata
  POST /job/<id>/approve    PENDING_REVIEW -> APPROVED (the publisher picks it up)
  POST /job/<id>/reanalyze  -> ANALYZING and restarts the pipeline from the Analyze
                            step (probe artifacts and transcript are reused)

Auth: shared token. Open <url>?token=<WEBUI_TOKEN> once, a cookie takes over.
Env: BUCKET, JOBS_TABLE, WEBUI_TOKEN, STATE_MACHINE_ARN.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import os
import re
from urllib.parse import parse_qs

import boto3

from autopublisher import storage
from autopublisher.models import (
    STATUS_ANALYZING,
    STATUS_APPROVED,
    STATUS_ERROR,
    STATUS_PENDING_REVIEW,
    Job,
)

COOKIE = "autopub_token"
COOKIE_MAX_AGE = 30 * 24 * 3600

STATUS_COLORS = {
    "ANALYZING": "#b58900",
    "PENDING_REVIEW": "#268bd2",
    "APPROVED": "#6c71c4",
    "PUBLISHING": "#2aa198",
    "DONE": "#859900",
    "ERROR": "#dc322f",
}

CSS = """
body{font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:960px;padding:1rem;
     background:#fdf6f0;color:#333}
a{color:#268bd2;text-decoration:none} a:hover{text-decoration:underline}
table{border-collapse:collapse;width:100%} td,th{padding:.4rem .6rem;text-align:left;
     border-bottom:1px solid #e5ded6}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:.6rem;color:#fff;
       font-size:.8rem;font-weight:600}
.card{background:#fff;border:1px solid #e5ded6;border-radius:.5rem;padding:1rem;
      margin:1rem 0}
.tags span{background:#eee8e0;border-radius:.4rem;padding:.05rem .45rem;margin-right:.3rem;
      font-size:.85rem}
video{max-width:270px;max-height:480px;background:#000;border-radius:.4rem}
.short{display:flex;gap:1rem;align-items:flex-start;flex-wrap:wrap}
button{background:#268bd2;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1rem;
       font-size:1rem;cursor:pointer}
button.green{background:#859900}
textarea{width:100%;box-sizing:border-box;min-height:4rem;font:inherit}
.error{background:#fff0f0;border:1px solid #dc322f;border-radius:.4rem;padding:.5rem 1rem;
       white-space:pre-wrap;word-break:break-all}
small{color:#888}
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# Thin wrappers so tests can stub the AWS edges out.

def presign(key: str) -> str:
    return boto3.client("s3").generate_presigned_url(
        "get_object", Params={"Bucket": os.environ["BUCKET"], "Key": key}, ExpiresIn=3600
    )


def start_reanalysis(job: Job) -> None:
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        input=json.dumps({
            "reanalyze": True,
            "job_id": job.job_id,
            "video_key": job.video_key,
            "prompt": job.prompt,
            "summary": {"transcript_key": f"work/{job.job_id}/transcript.json"},
        }),
    )


# -- Responses ----------------------------------------------------------------

def respond(status: int, body: str, cookies: list[str] | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "cookies": cookies or [],
        "body": body,
    }


def redirect(path: str, cookies: list[str] | None = None) -> dict:
    return {"statusCode": 303, "headers": {"Location": path}, "cookies": cookies or [], "body": ""}


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head>"
            f"<body><h1><a href='/'>autopublisher</a></h1>{body}</body></html>")


def badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#888")
    return f"<span class='badge' style='background:{color}'>{esc(status)}</span>"


# -- Views --------------------------------------------------------------------

def render_index(jobs: list[Job]) -> str:
    if not jobs:
        return page("Jobs", "<p>No jobs yet — upload a video under <code>incoming/</code>.</p>")
    rows = "".join(
        f"<tr><td><a href='/job/{esc(j.job_id)}'>{esc(j.job_id)}</a></td>"
        f"<td>{badge(j.status)}</td><td>{esc(j.created_at)}</td>"
        f"<td>{len(j.shorts)}</td><td>{esc(j.language or '—')}</td></tr>"
        for j in jobs
    )
    table = (f"<table><tr><th>Job</th><th>Status</th><th>Created</th>"
             f"<th>Shorts</th><th>Language</th></tr>{rows}</table>")
    return page("Jobs", table)


def tags_html(tags: list[str]) -> str:
    return "<div class='tags'>" + "".join(f"<span>{esc(t)}</span>" for t in tags) + "</div>"


def render_job(job: Job) -> str:
    parts = [f"<h2>{esc(job.job_id)} {badge(job.status)}</h2>",
             f"<p><small>{esc(job.video_key)} · created {esc(job.created_at)} · "
             f"language {esc(job.language or 'unknown')}</small></p>"]

    if job.error:
        parts.append(f"<div class='error'>{esc(job.error)}</div>")

    link = job.main_video_link()
    main_link = f"<p><a href='{esc(link)}'>{esc(link)}</a></p>" if link else ""
    parts.append(
        f"<div class='card'><h3>Main video</h3>"
        f"<p><strong>{esc(job.main.title) or '<em>no title yet</em>'}</strong></p>"
        f"<p>{esc(job.main.description)}</p>{tags_html(job.main.tags)}{main_link}</div>"
    )

    for short in job.shorts:
        video = (f"<video controls preload='metadata' src='{esc(presign(short.s3_key))}'>"
                 "</video>" if short.s3_key else "<p><em>not rendered yet</em></p>")
        published = (f"<p>Published: <a href='https://youtu.be/{esc(short.youtube_id)}'>"
                     f"youtu.be/{esc(short.youtube_id)}</a> at {esc(short.published_at)}</p>"
                     if short.youtube_id else "")
        problems = short.validate()
        warn = (f"<div class='error'>{esc('; '.join(problems))}</div>" if problems else "")
        parts.append(
            f"<div class='card short'><div>{video}</div><div style='flex:1;min-width:16rem'>"
            f"<h3>{esc(short.short_id)}: {esc(short.title)}</h3>"
            f"<p><small>{short.start:.1f}s → {short.end:.1f}s "
            f"({short.duration:.0f}s)</small></p>"
            f"<p>{esc(short.description)}</p>{tags_html(short.tags)}"
            f"<p><em>{esc(short.reason)}</em></p>{published}{warn}</div></div>"
        )

    job_path = f"/job/{esc(job.job_id)}"
    if job.status == STATUS_PENDING_REVIEW:
        parts.append(
            f"<div class='card'><form method='post' action='{job_path}/approve'>"
            f"<button class='green'>Approve — start publishing</button></form></div>"
        )
    if job.status in (STATUS_PENDING_REVIEW, STATUS_ERROR):
        parts.append(
            f"<div class='card'><form method='post' action='{job_path}/reanalyze'>"
            f"<p>Guidance for the new analysis (optional):</p>"
            f"<textarea name='prompt'>{esc(job.prompt)}</textarea><br><br>"
            f"<button>Re-analyze</button></form></div>"
        )
    return page(job.job_id, "".join(parts))


# -- Actions --------------------------------------------------------------------

def form_fields(event) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


def do_approve(job: Job) -> dict:
    try:
        job.transition(STATUS_APPROVED)
    except ValueError as exc:
        return respond(409, page("Conflict", f"<p>{esc(exc)}</p>"))
    storage.save_job(job)
    return redirect(f"/job/{job.job_id}")


def do_reanalyze(job: Job, prompt: str) -> dict:
    try:
        job.transition(STATUS_ANALYZING)
    except ValueError as exc:
        return respond(409, page("Conflict", f"<p>{esc(exc)}</p>"))
    job.prompt = prompt
    job.error = ""
    storage.save_job(job)
    start_reanalysis(job)
    return redirect(f"/job/{job.job_id}")


# -- Entrypoint ---------------------------------------------------------------

def handler(event, context):
    token = os.environ["WEBUI_TOKEN"]
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath") or "/"

    query = event.get("queryStringParameters") or {}
    if hmac.compare_digest(query.get("token", ""), token):
        cookie = (f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Lax; "
                  f"Path=/; Max-Age={COOKIE_MAX_AGE}")
        return redirect(path, cookies=[cookie])

    sent = ""
    for raw in event.get("cookies") or []:
        name, _, value = raw.partition("=")
        if name == COOKIE:
            sent = value
    if not hmac.compare_digest(sent, token):
        return respond(403, page("Denied", "<p>Open the link with <code>?token=…</code> "
                                           "from the Terraform output.</p>"))

    if path == "/" and method == "GET":
        return respond(200, render_index(storage.list_jobs()))

    match = re.fullmatch(r"/job/([a-z0-9-]+)(/approve|/reanalyze)?", path)
    if not match:
        return respond(404, page("Not found", "<p>No such page.</p>"))
    job = storage.load_job(match.group(1))
    if job is None:
        return respond(404, page("Not found", "<p>No such job.</p>"))
    action = match.group(2)

    if action is None and method == "GET":
        return respond(200, render_job(job))
    if action == "/approve" and method == "POST":
        return do_approve(job)
    if action == "/reanalyze" and method == "POST":
        return do_reanalyze(job, form_fields(event).get("prompt", "").strip())
    return respond(405, page("Method not allowed", "<p>Wrong method.</p>"))
