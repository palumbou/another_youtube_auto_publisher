# ADR 0003 — OpenTofu stays; one Fargate task runs a whole revision

Date: 2026-09-20. Status: accepted.

## IaC tool

The repository's `infra/` was applied with OpenTofu 1.12.3 (the lock file points at `registry.opentofu.org`). Keeping Terraform-syntax modules and OpenTofu avoids a migration and matches the established tool; no new tool was introduced. Validation for this programme used OpenTofu 1.12.6, downloaded to a scratch directory and checksum-verified.

## Worker shape

The old pipeline split probe, transcribe, analyse and cut across Lambdas and two Fargate runs. A revision now runs as one Fargate task (`MODE=process`) that owns validation, transcript, analysis, rendering and verification and stops at `AWAITING_REVIEW`. Reasons: ffmpeg and the media files live in one place, the providers are behind interfaces anyway, and Step Functions only has to run one task per revision with retries limited to placement errors. The ingest Lambda stays small and has no ffprobe: it records the manifest's declared media facts and the worker re-validates them first.

## Alternatives rejected

- Lambda with an ffmpeg layer for rendering: 15-minute limit and /tmp size make long masters fragile.
- Keeping separate probe and cut tasks: two container starts per revision and artifacts handed through S3 for no benefit.
