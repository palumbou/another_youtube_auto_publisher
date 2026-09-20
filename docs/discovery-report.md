# Discovery report — another-youtube-auto-publisher

Date: 2026-09-20. Scope: the state of this repository before the three-way programme touches it.

## Repository facts

| Item | Value |
|---|---|
| Remote | `https://github.com/palumbou/another_youtube_auto_publisher` (public, default branch `master`) |
| HEAD at discovery | `4088c1d768d7dc951ae17c3948f432860a1719fe` (2026-07-05) |
| History | 2 commits, no tags, no other branches |
| Language / runtime | Python, `requires-python >=3.12`; no runtime dependencies in the package itself, `boto3` optional for AWS |
| Tooling | hatchling build, pytest, ruff (line length 100) |
| Infrastructure | Terraform-syntax modules in `infra/`, applied with OpenTofu 1.12.3 (`registry.opentofu.org` providers: aws 6.53.0, archive 2.8.0, random 3.9.0) |
| Container | `worker/Dockerfile` (ffmpeg worker for Fargate) |
| CI | none |
| Licence | MIT |
| Local copies | see `repository-reconciliation-report.md` |

## What the code does today

- `autopublisher/models.py` — `Job`, `Short`, `MainVideo` dataclasses; six free-form status strings with a transition table (`ANALYZING → PENDING_REVIEW → APPROVED → PUBLISHING → DONE`, `ERROR`); YouTube metadata sanitising.
- `autopublisher/pipeline.py` — Step Functions glue Lambda: `intake` creates a job from **any** S3 object created under `incoming/` with a video extension; there is no `READY` marker, no `cutover_at`, no manifest, no hash or version check, and job ids derive from the key alone (`sha1(key)[:8]`).
- `autopublisher/analyze.py` — Bedrock (Claude vision) analysis of frame mosaics + transcript, producing Shorts plan and English metadata.
- `autopublisher/publish.py` — scheduled Lambda: uploads the main video **unlisted** automatically after approval, then one Short per day, then flips the main video **public**. Approval is a single job-level action.
- `autopublisher/youtube.py` — stdlib-only YouTube Data API client with resumable uploads streamed from S3.
- `autopublisher/webui.py` — server-rendered review UI on a Lambda Function URL, protected by a single bearer token in the URL/cookie. No IP allowlist, no login.
- `worker/worker.py` — ffmpeg probe (scene changes, loudness, VAD, mosaics) and cut (9:16 with blurred background).
- `scripts/authorize.py` — one-time desktop OAuth flow for the refresh token; stored in Secrets Manager.
- `tests/` — 56 tests over the pure layers (model, plan cleaning, YouTube protocol, web UI routing). Executed: PASS.

## Gap analysis against `04_AUTO_PUBLISHER_SPEC.md`

| Requirement | Today | Gap |
|---|---|---|
| `READY` marker as the only trigger, `cutover_at`, allowed prefix/project | any video object triggers | missing |
| Manifest contract 1.0.0, schema validation, manifest precedence | optional `<video>.json` sidecar with `youtube_id`, `language`, `prompt` | missing; the `youtube_id` sidecar path (publishing Shorts for an existing channel video) contradicts the "no backfill" rule and must be retired |
| Idempotency key with hash + S3 version id, conditional writes | `put_item` without condition | missing |
| Explicit state machine with revisions, scoped reprocess, approval per asset | 6 statuses, whole-job approve, "re-analyze" only | partial |
| Review console with IP allowlist + personal auth | token in URL | insufficient |
| Private-first upload, per-asset approval, no automatic public flip | unlisted → public automatically | contradicts spec |
| Audit log, optimistic locking | none | missing |
| Local development with fixtures and fake YouTube | worker only | partial |
| Contract fixtures in CI | none | missing |

## Decisions taken for the workstream

- Keep Python and the existing pure-domain/tests layout; keep Terraform-syntax IaC and OpenTofu as the established tool (ADR-free: it is what the repository already uses).
- Replace the intake path rather than patch it: new ingestion module driven by `READY`, manifest and cutover; the sidecar/`youtube_id` path is removed.
- Introduce a versioned job/revision model with the spec's states; the old six-status model is kept only as far as the tests that still describe useful metadata rules.
- Review console: keep server-rendered simplicity but require a signed-in session (Cognito in AWS, seeded local user only in `LOCAL_MODE`), and add the WAF IP-set in IaC.
- All real publishing stays behind explicit approval and a `PUBLISH_KILL_SWITCH`.
