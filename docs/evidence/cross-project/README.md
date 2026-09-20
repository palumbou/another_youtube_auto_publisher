# Cross-project acceptance — Quiz al Volo job `qav-pilota-001`

Run by the programme coordinator on 2026-09-20 against the local stack
(filesystem bucket root `handoff/s3-local`, state root `handoff/publisher-state`,
fake YouTube adapter, mock providers driven by the manifest). The input is a
**newly rendered** Quiz al Volo master (296.6 s, 1920×1080, SHA-256
`69dc69ed…ccee3`, manifest `e2e9175…5ab5`), not an existing channel video.

| Step (`01_PROMPT_COORDINATORE_AGENT.md` §11 milestone 3) | Result | Evidence |
|---|---|---|
| 1. Source + manifest uploaded to `incoming/quiz-al-volo/qav-pilota-001/`, `READY` last | done by the Quiz al Volo delivery tool | `qav-pilota-001-cli-log.txt` |
| 2. Idempotent ingestion | first `ingest` → `CREATED`; second → `DUPLICATE` / `DUPLICATE_JOB` | log, `qav-pilota-001-rev1-status.json` |
| 3. Transcript/analysis and manifest precedence | revision 1 reached `AWAITING_REVIEW`, 3 Shorts cut exactly on manifest segments (question→countdown→answer→explanation), e.g. tennis 7 900–33 200 ms = 25 300 ms | `rev1-status`, `work/…/rev-0001/revision.json` (not committed, media) |
| 4. At least one valid Short | 3 Shorts 1080×1920 H.264/AAC, loudness −13.8/−14.8/−14.2 LUFS, true peak ≤ −1.2 dBTP, no black bars | `rev-0001` quality blocks |
| 5. Reject one revision with a reason | rejected with reason "Short crop cut the card text and captions overlapped the on-frame text (revision 1)", scope `SHORTS_ONLY` | audit seq 6 |
| 6. Reprocess only the requested scope | revision 2 with `scope=SHORTS_ONLY`, transcript and analysis keys reused, Shorts re-rendered with the fixed layout (crop 1080×1080 @420,0, scale 1.0, captions skipped because the master already shows the text) | audit seq 7–8, revision 2 |
| 7. Approve master and one Short | confirmations recorded (audit 9–10), `BATCH_APPROVED` (11); two Shorts left `PENDING` and never uploaded | `qav-pilota-001-final-status.json` |
| 8. Queue | two schedules, `2027-01-01T10:00` Europe/Rome → `09:00:00Z`, privacy after publish `private`, notify off | log |
| 9. YouTube adapter in mock mode | `publish --now 2027-01-01T10:00:00Z` → master `fake-1fe0f299`, Short `fake-2394c7af`, both `private`, processing `succeeded`; real-upload mode NOT RUN (no authorization, no OAuth bootstrap) | log, `publications/` |
| 10. Audit, job states, checksums, no duplicate upload | final state `UPLOADED_PRIVATE`, 18 audit events; second `publish` run 5 minutes later returned `[]` and created no publication | final status |

Two defects found during this run were fixed on `feat/aws-infrastructure`
before step 6 and step 7 respectively: `be97a0e` (crop and captions),
`c41f0c4` (CLI confirm for Short assets). Local absolute paths in the log are
shortened to the `handoff/…` form.
