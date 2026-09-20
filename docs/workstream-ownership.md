# Workstream ownership map — three-project programme

Published on 2026-09-20 before any parallel change. One workstream owns one checkout; nobody else writes into it.

| Workstream | Repository | Local canonical checkout | Active branch(es) | Owns |
|---|---|---|---|---|
| Quiz on the Fly | `https://github.com/palumbou/quiz_on_the_fly` (private, created empty by the owner on 2026-09-20; reused instead of creating a second `quiz-on-the-fly` repository) | `/home/fedora/Desktop/claude/quiz_on_the_fly` | `feat/qotf-foundation` → `feat/qotf-homeward-journey` → `feat/qotf-crazygames` → `feat/qotf-site` | English 3D Unity WebGL game, CrazyGames adapter, its site, its content catalogue |
| Quiz al Volo | `https://github.com/palumbou/quiz_al_volo` (private) | worktree `/home/fedora/Desktop/claude/quiz_al_volo_youtube` (main checkout `/home/fedora/Desktop/claude/quiz_al_volo` stays on `feat/quiz-on-the-fly-core` and is not touched) | `feat/quiz-al-volo-youtube` from `7b85ed6` | Italian catalogue, editorial workflow, master-video renderer, manifest producer, S3 delivery, Italian site |
| another-youtube-auto-publisher | `https://github.com/palumbou/another_youtube_auto_publisher` (public) | `/home/fedora/Desktop/claude/another-youtube-auto-publisher_reconciled` | `chore/reconcile-local-copies` → `feat/s3-media-ingestion` → `feat/media-analysis-and-shorts` → `feat/review-console` → `feat/youtube-publishing` → `feat/aws-infrastructure` | S3 ingestion, analysis, Shorts, review console, queue, YouTube upload, AWS IaC |

Shared, read-only for everyone: the plan package `/home/fedora/Desktop/claude/quiz_three_projects_2026-09-19/` (contract 1.0.0, schema, fixtures). Each repository carries its own copy of the schema and fixtures under `contracts/`; changes go through the contract process in `06_CONTRATTO_INTEGRAZIONE.md`.

Originals that nobody writes to: `/home/fedora/Desktop/claude/another-youtube-auto-publisher_downloads`, `/home/fedora/Desktop/claude/another-youtube-auto-publisher_downloads.tar.gz`, the reconciliation backups.

Shared rules: Conventional Commits, one atomic commit per complete logical change, immediate push, no `Co-Authored-By`, no AI attribution in commits or code, the generic README disclosure at the very top of each main README, no secrets in Git, no public action (submission, real publication, DNS, paid deploy, visibility change) without a current explicit authorization.
