# Repository reconciliation report — another-youtube-auto-publisher

Executed on **2026-09-20** (UTC run stamp `20260920T084457Z`), following
`05_RUNBOOK_RICONCILIAZIONE_REPOSITORY.md` of the three-way plan package.

## Host

| Item | Value |
|---|---|
| Operating system | Fedora Linux 44 (Cloud Edition), kernel `7.2.5-200.fc44.x86_64` |
| Note | The plan names Ubuntu; the host actually found is Fedora. Paths were used exactly as supplied and not "corrected". |
| Filesystem | btrfs, `/home` with 95 GB free at run time |
| User | `fedora` (uid 1000), owner of every path involved; no `sudo` was used |
| Git | 2.55.0 |

## Supplied paths and what was found

| Supplied | Resolved | State |
|---|---|---|
| Copy A `/home/fedora/Desktop/claude/another-youtube-auto-publisher_downloads` | Git top level is exactly that path | **Present** |
| Copy B `/home/fedora/Desktop/claude/another-youtube-auto-publisher_sync` | — | **Absent**: the directory does not exist on this host. Recorded in `b/MISSING.txt` of the backup. The owner's prompt for this run listed only copy A and the remote. |
| Expected remote `https://github.com/palumbou/another_youtube_auto_publisher` | Reachable; default branch `master`; the repository is **public** | **Present** |
| Extra evidence `/home/fedora/Desktop/claude/another-youtube-auto-publisher_downloads.tar.gz` | 192 MB tarball dated 2026-09-20 10:01 local, SHA-256 `6b02bfe3…e7b25` | Treated as a snapshot of copy A and compared, see below |

## Remotes

| Copy | Fetch / push URL | Same repository as expected? |
|---|---|---|
| A | `git@github.com:palumbou/another_youtube_auto_publisher.git` (SSH) | Yes — SSH spelling of the expected HTTPS repository |
| Reconciled clone | `https://github.com/palumbou/another_youtube_auto_publisher.git` | Yes |

SSH authentication is not available on this host (`Permission denied (publickey)`), so the remote was fetched by explicit HTTPS URL without changing copy A's remote configuration. The temporary ref created by that fetch (`refs/fetched-https/master`) was deleted afterwards; copy A's ref set is exactly what it was before the run.

## Before and after HEAD

| Ref | Before | After |
|---|---|---|
| Copy A `master` | `4088c1d768d7dc951ae17c3948f432860a1719fe` | unchanged |
| Copy A `origin/master` | `4088c1d768d7dc951ae17c3948f432860a1719fe` | unchanged |
| Remote `refs/heads/master` (ls-remote) | `4088c1d768d7dc951ae17c3948f432860a1719fe` | unchanged |
| Tarball `.git` HEAD | `4088c1d768d7dc951ae17c3948f432860a1719fe` | n/a |

## Inventories (copy A)

- Branches: `master` only; `origin/master`, `origin/HEAD`.
- Tags: none (locally and on the remote).
- Stash: empty.
- Submodules: none. Git LFS: no pointers.
- Object integrity: `git fsck --full` reports one dangling blob `db359ecd…`, 32 632 bytes, which is a previous `git add` of `infra/deploy.tfplan` (identical size and zip header). Nothing is missing or corrupt.
- Staged changes: none (`staged.patch` is empty, SHA-256 `e3b0c442…`).
- Unstaged changes: none (`unstaged.patch` is empty).
- Untracked, not ignored: `infra/deploy.tfplan` (an OpenTofu saved plan, 32 632 bytes, zip). Archived in `a/untracked.tar.gz`, mode 600. **Not imported** into the canonical checkout: it is a generated artefact of a July 2026 apply, and saved plans can embed variable values.
- Ignored files present (names only, no values were read or copied): `infra/terraform.tfstate`, `infra/terraform.tfstate.backup`, `infra/terraform.tfvars`, `infra/.terraform.lock.hcl`, `infra/.terraform/providers/…`, `infra/build/autopublisher.zip`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`.
  - The state file (serial 4, `terraform_version` 1.12.3, lineage `92dc46d3…`) shows that the Terraform/OpenTofu stack in `infra/` **was applied** on 2026-07-05 in `eu-west-1`: S3 bucket, DynamoDB table, four Lambdas, a Lambda Function URL, ECS cluster and task definition, ECR repository, Step Functions state machine, EventBridge rules, IAM roles, log groups and a random web-UI token. Whether those resources still exist cannot be checked from this host: no AWS credentials are configured (`aws sts get-caller-identity` → `NoCredentials`). This is recorded as an **open item with possible ongoing cost** (ECR image storage, log retention; the rest is pay-per-use).
  - The state file and the tfvars file contain the web-UI access token and deployment parameters. They stay where they are, are never copied into the canonical checkout, and are not part of any archive that leaves this host.

## Ancestry and content comparison

```
copy A master ──┐
                ├── 4088c1d  (identical commit)
origin/master ──┘
tarball HEAD ───── 4088c1d
```

| Check | Result |
|---|---|
| `merge-base --is-ancestor local-copy-a/master origin/master` | true |
| `merge-base --is-ancestor origin/master local-copy-a/master` | true |
| `rev-list --left-right --count local-copy-a/master...origin/master` | `0 0` |
| Tracked-file SHA-256 set, copy A vs fresh clone | identical (38 files) |
| Content inventory, copy A vs tarball (excluding `.git`, `infra/.terraform`, caches) | identical (`inventory-sha256.txt` hashes match: `eeccf92d…`) |

Classification (runbook phase 5): **category 1 — identical tracked commit and clean trees**, plus **category 7 — generated/cache-only differences** (`deploy.tfplan`, caches, provider binaries, state) which are intentionally excluded. No unique commits exist anywhere. No user source file is unaccounted for.

Unique commit table: empty. The whole history is two commits, both on the remote:

| SHA | Subject |
|---|---|
| `a60342c27fb2e1b04fc880154d51c8be0ce05b12` | Initial release: automated YouTube publishing pipeline on AWS |
| `4088c1d768d7dc951ae17c3948f432860a1719fe` | Fix worker image build: webrtcvad needs gcc to compile |

## Backup

Directory `/home/fedora/Desktop/claude/reconciliation-backups/20260920T084457Z/` (mode 700, owner `fedora`):

| File | SHA-256 |
|---|---|
| `a/repository.bundle` (all refs, verified with `git bundle verify`) | `fe37de3d0943b0802280c5ce29965b082fa3b8ce6ec587fa33746139dec0d215` |
| `a/staged.patch` (empty) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `a/unstaged.patch` (empty) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `a/untracked.tar.gz` (`infra/deploy.tfplan`) | `55daf227a9447f0236d77be074e2ab9f5facb8a1bf86d77b8db42f68e609b841` |
| `a/inventory-sha256.txt` | `eeccf92dc5fb47151c034a42148615e661a09058cd185e98cba8f9f3ae923310` |
| `a/inventory-meta.tsv` | `56f0d8282bc7e74fe481d951cefa35ef5643ad303527f29be71b46fb7f99f6fc` |
| `a/inventory-excluded-dirs.txt` | `cbc13a93484b38ecb1d876a4c031710fe87cdfe65c80e9a95ac9c4f260b9dcef` |
| `tarball/inventory-sha256.txt` | `eeccf92dc5fb47151c034a42148615e661a09058cd185e98cba8f9f3ae923310` |

Also kept there: `status.txt`, `status-v2.z`, `remotes.txt`, `branches.txt`, `tags.txt`, `log.txt`, `reflog.txt`, `ignored.txt`, `stash.txt`, `fsck.txt`, `ls-remote.txt`, `refs-post-fetch.txt`, `b/MISSING.txt`, the tarball's extracted tree (without provider binaries) and `SHA256SUMS`. The backup is never pushed.

## Canonical checkout

`/home/fedora/Desktop/claude/another-youtube-auto-publisher_reconciled` — a fresh clone of the remote (runbook phase 6), with copy A added as the temporary remote `local-copy-a` for the ancestry checks. Chosen because the remote, copy A and the tarball are the same commit with identical trees, so the cleanest lineage is the remote itself; the originals stay untouched as recovery sources. Branch `chore/reconcile-local-copies` starts at `4088c1d`.

Archive branches or tags: **none needed** — there is no unique committed work to preserve.

Patches imported: none (there were none). Files imported: none. Files intentionally excluded: `infra/deploy.tfplan`, state, tfvars, lock file, caches, provider binaries, `infra/build/autopublisher.zip`.

Conflicts: none.

## Validation of the canonical state (runbook phase 9)

| Check | Status | Evidence |
|---|---|---|
| Clean working tree | PASS | `git status --short` empty on `chore/reconcile-local-copies` |
| No missing unique commit | PASS | `rev-list --left-right --count` = `0 0` |
| No unaccounted user source file | PASS | tracked hash sets identical; the only untracked file is classified above |
| No committed secret or media | PASS | tracked file list reviewed; `.gitignore` already excludes tfstate/tfvars/tokens/media |
| Clone-from-remote | PASS | this checkout is that clone |
| Unit tests | PASS | `uv venv .venv --python 3.14 && uv pip install -e ".[dev]" && .venv/bin/pytest -q` → `56 passed in 1.24s` (pytest 9.1.1, Python 3.14.7; `requires-python >=3.12`) |
| Lint | FAIL (pre-existing) | `.venv/bin/ruff check .` → 10 findings (ruff 0.16.8): 3× I001 import order, 6× ISC004 implicit string concatenation, 1× RUF100. None is a behaviour bug; fixed in a later commit on the feature line. |
| Application starts in local mode | PASS | `python worker/worker.py probe --local …` is the documented local path; exercised in the feature workstream with a generated fixture |

## Pushes performed

None in this phase before this report. The report itself is committed on `chore/reconcile-local-copies` and pushed to `origin`; the push and the ref are recorded in `docs/release-readiness.md`.

## Remaining blockers

- Copy B never existed on this host; if it exists elsewhere, run the runbook again against it before trusting this report as complete.
- The July 2026 AWS stack may still be deployed: no credentials are available to confirm or to estimate cost. Owner action: `tofu state list` / console check in `eu-west-1`.
- The remote repository is public. Making it private, or keeping it public, is the owner's decision; nothing in this run changed visibility.

## Statement

Both supplied original directories were left untouched: copy A was only read (plus a fetch whose only side effect, a temporary ref, was removed), and copy B was absent. Nothing was deleted, reset, cleaned, stashed, rebased or overwritten. The temporary `local-copy-a` remote is removed from the canonical clone once the report is committed.
