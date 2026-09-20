# Release readiness — another-youtube-auto-publisher

Date: 2026-09-20. Host: Fedora Linux 44, Python 3.14.7, ffmpeg 8.1.2 (ffmpeg-free, H.264 via libopenh264), OpenTofu 1.12.6 (scratch download).

## Branch and commit inventory

Stacked branches, each containing the previous one; all pushed to `origin` (`git ls-remote` verified after every push, see the handoff table):

| Branch | Tip |
|---|---|
| `chore/reconcile-local-copies` | `ddeb048` |
| `feat/s3-media-ingestion` | `8c02321` |
| `feat/media-analysis-and-shorts` | `374826b` |
| `feat/review-console` | `0c4d6a7` |
| `feat/youtube-publishing` | `17b6b3f` |
| `feat/aws-infrastructure` | `e648ee1` (this document lands on top) |

Commits since `master` (`4088c1d`), oldest first:

| SHA | Subject | Author |
|---|---|---|
| `4592b87` | docs(reconcile): record the reconciliation of the local copies and the remote | Palumbo U |
| `8db3e80` | chore(git): enforce conventional commits without ai attribution | Palumbo U |
| `6d09018` | docs(readme): add the ai-assisted development disclosure | Palumbo U |
| `62c8382` | style: fix import order and implicit string concatenation | Palumbo U |
| `ddeb048` | feat(contract): add the video job manifest contract 1.0.0 and fixtures | Palumbo U |
| `f24035e` | fix(contract): track the tiny fixture video in spite of the media rule | Palumbo U |
| `80fb3e0` | feat(core): add settings, object store, job states and stores | Palumbo U |
| `bdea2f3` | feat(ingest): create jobs only from post-cutover READY markers | Palumbo U |
| `8c02321` | refactor(pipeline): retire the sidecar intake for ready ingestion | Palumbo U |
| `8810c36` | feat(providers): add transcription and analysis providers with grounding | Palumbo U |
| `97e2c95` | feat(shorts): plan manifest-first shorts and render them with ffmpeg | Palumbo U |
| `374826b` | feat(processing): run immutable revisions up to awaiting review | Palumbo U |
| `7a66e95` | feat(review): add version-checked review actions with an audit trail | Palumbo U |
| `0c4d6a7` | feat(console): replace the token web ui with an authenticated console | Palumbo U |
| `c82fd08` | feat(youtube): carry reviewed metadata, publishAt and typed errors | Palumbo U |
| `1f6f251` | feat(publish): gate uploads on approval, kill switch and idempotency | Palumbo U |
| `17b6b3f` | feat(local): add the one-command local stack and the e2e flow | Palumbo U |
| `284cdcb` | feat(ingest): support probe-less ingestion and worker failure recording | Palumbo U |
| `e648ee1` | feat(infra): rebuild the stack around ready ingestion and review | Palumbo U |

No pull request was opened and `master` was not touched: merging is the owner's decision.

## Tests

| Check | Status | Command / evidence |
|---|---|---|
| Lint | PASS | `.venv/bin/ruff check .` → `All checks passed!` |
| Unit + integration suite | PASS | `.venv/bin/pytest -q` → `326 passed in 227.80s (0:03:47)` (`docs/evidence/pytest-final.txt`) |
| Contract fixtures (schema Draft 2020-12, invalid fixtures with stable codes) | PASS | `tests/test_contract.py` (15 tests) |
| Duplicate / out-of-order READY events → one job | PASS | `tests/test_ingest.py::test_duplicate_and_out_of_order_events_create_one_job` |
| Pre-cutover READY ignored; missing READY ignored | PASS | `test_pre_cutover_ready_is_ignored`, `test_missing_ready_is_ignored` |
| Hash mismatch, changed source version, schema/semantic failures, corrupt/oversized media, unexpected objects | PASS | `tests/test_ingest.py` |
| Every valid and invalid state transition (14 × 14 pairs) | PASS | `tests/test_domain.py::test_every_transition_pair` |
| Conditional writes and optimistic locking | PASS | `tests/test_jobstore.py` |
| Provider failure, low-confidence transcript kept and flagged, hallucinated claim rejected, invalid structured output rejected | PASS | `tests/test_providers.py`, `tests/test_processing.py` |
| Manifest precedence and Short completeness (question→answer→explanation) | PASS | `tests/test_shorts.py`, contract fixture `invalid-short-cuts-explanation.json` |
| ffmpeg output 1080×1920, h264/aac, duration, no black bars, true peak | PASS | `tests/test_render.py::test_render_short_from_fixture` (real ffmpeg run) |
| Crop honours the manifest `crop_region` pixel-exactly (no zoom, no offset): synthetic 1920×1080 master with a marker at x=700 lands at x=280 in the Short (1:1 central crop) and at x≈85–121 for a narrow region scaled to fit; measured on decoded rows | PASS | `tests/test_render.py::test_marker_lands_where_the_layout_says` (regression for the defect found in the cross-project test) |
| Burned captions never cover text the master already shows; decision per caption logged in the render report | PASS | `tests/test_render.py::test_captions_never_cover_master_text_and_decisions_are_logged`; real job: `docs/evidence/qav-pilota-001-rev1-tennis-render-report.json` |
| Descriptions never spoil answers or explanations | PASS | `tests/test_providers.py::test_description_never_spoils_answers_or_explanations`, `tests/test_processing.py` |
| Re-render of the real Quiz al Volo job `qav-pilota-001` (296.6 s master, 3 Shorts) with the fixed renderer, in a private bucket/state root | PASS | frames at 20 s of source time before and after: `docs/evidence/qav-pilota-001-short-tennis-t20s-before-fix.png`, `docs/evidence/qav-pilota-001-short-tennis-t20s-after-fix.png`; status `docs/evidence/qav-pilota-001-rerender-status.json` |
| Similarity / volume gate | PASS | `tests/test_shorts.py` |
| Scoped reprocess creates immutable next revision | PASS | `tests/test_processing.py::test_scoped_reprocess_creates_immutable_next_revision` |
| Approval required before scheduling/upload; batch approval leaves unselected shorts pending | PASS | `tests/test_review.py`, `tests/test_publishing.py::test_unapproved_asset_is_refused_even_if_scheduled` |
| Concurrent review conflict (409) | PASS | `tests/test_review.py::test_concurrent_review_conflict`, `tests/test_console.py::test_full_review_flow_through_http` |
| Login required, wrong password, IP allowlist fails closed, JWT authenticator fails closed | PASS | `tests/test_console.py` |
| Kill switch, daily cap, quota, auth revocation, retry without duplicate upload, no public flip, private-only for unaudited project | PASS | `tests/test_publishing.py` |
| YouTube client: resumable upload, 308 continuation, publishAt only with private, typed auth/quota errors | PASS | `tests/test_youtube.py` |
| End-to-end on a newly generated fixture (ingest ×2, reject METADATA_ONLY, reprocess, approve, schedule, fake upload ×2, UPLOADED_PRIVATE) | PASS | `python -m autopublisher.local e2e` → `docs/evidence/e2e-local-run.json` (41.7 s) |
| Local stack starts and serves the console | PASS | `docs/evidence/serve-smoke.log` (healthz 200, login 303, wrong password 401) |
| OpenTofu fmt + validate | PASS | `docs/evidence/tofu-validate.txt` |
| OpenTofu plan / apply | BLOCKED | no AWS credentials on this host and no deployment authorization or recorded budget |
| Worker container image build | NOT RUN | no Docker daemon in this session (podman present, build not attempted); the Dockerfile is the only untested artefact |
| Real YouTube private upload | NOT RUN | requires the owner's OAuth bootstrap and an authorized environment |
| Cross-project acceptance with a real Quiz al Volo master | PASS (coordinator) | ingest CREATED, duplicate DUPLICATE_JOB, revision 1 AWAITING_REVIEW with 3 Shorts on `/home/fedora/Desktop/claude/handoff/s3-local`; the Short crop defect it revealed is fixed and covered above |
| WAF/auth configuration on a live stack, DLQ redrive, backup restore | NOT RUN | need a deployment |

## Build sizes and checksums

| Artefact | Size | SHA-256 |
|---|---|---|
| `tests/fixtures/tiny.mp4` (committed, generated by ffmpeg testsrc2+sine) | 48744 bytes | `1437076033669afa5030f7510e9eecedd21e76ee12c4d2e50837fec4c323817b` |
| e2e Short `short-01.mp4` (not committed) | see `docs/evidence/e2e-local-run.json` | first 12 hex of SHA-256 recorded per revision in the report |
| Lambda package | built by `data.archive_file` at apply time | n/a |

Tracked files: 85.

## Dependency and licence inventory

See `THIRD_PARTY_NOTICES.md`. Runtime: jsonschema (MIT). Optional: boto3 (Apache-2.0). Dev: pytest, ruff, rfc3339-validator (MIT). Media: FFmpeg (LGPL build here; libx264 GPL in the worker image), OpenH264 (BSD), Noto Sans / Liberation Sans (OFL 1.1).

## Security findings

- No secret is committed: staged diffs were grepped before every push; `.gitignore` excludes tokens, tfstate, tfvars, media; the local password is generated per run and printed once.
- The copy-A `infra/terraform.tfstate` and `terraform.tfvars` contain the old web-UI token and deployment values: they stay on the workstation, are not in this repository and are not in the reconciliation archive that leaves the host (none does).
- The old shared-token Function URL is removed; the console requires Cognito + JWT authorizer + WAF allowlist, and the app rejects an empty allowlist outside local mode.
- Only the publish role can read the YouTube secret; the console role cannot.
- Open: the remote repository is public; it now contains the infrastructure layout and the review console code. Nothing sensitive, but the owner may prefer private.

## Performance evidence

Real job: 296.6 s 1080p master → ingest + FULL revision with three 1080×1920 Shorts and three thumbnails in 4 min 10 s wall clock (libopenh264, single workstation). Local e2e: 42 s 1080p master → ingest + FULL revision (transcript mock, analysis mock, one 1080×1920 Short with captions and loudness normalisation, thumbnail, verification) + METADATA_ONLY revision + two fake uploads in 41.7 s wall clock on a workstation with libopenh264. Test suite: 326 tests in 228 s, dominated by real ffmpeg renders.

## Open blockers

1. Deployment authorization, AWS credentials and a budget are needed before `tofu plan/apply`; the July 2026 stack may still exist and shares resource names.
2. YouTube OAuth bootstrap (one-time owner action) and the Google API project audit status are unknown; until audited, the honest ceiling is `UPLOADED_PRIVATE`.
3. The worker image has not been built in this session.
4. Bedrock/Transcribe providers are implemented against the documented APIs but were exercised only through their interfaces (no credentials).
5. Prices in `docs/cost-estimate.md` are largely unverified (client-side pricing tables).

## Verdict

**READY FOR INTERNAL REVIEW.** The local stack processes a newly generated job to `AWAITING_REVIEW`, the console supports playback, edits, approval, rejection and scoped reprocessing, the mock publish completes idempotently, and the security and state-transition tests pass. It is not ready for a private platform test until the infrastructure is applied under authorization and the OAuth bootstrap has been done.
