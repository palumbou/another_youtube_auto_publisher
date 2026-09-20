# Operations runbook

All actions below are owner actions on an authorized deployment. Nothing here is automatic.

## Kill switch

`publish_kill_switch = true` (default) makes the publish Lambda return `BLOCKED` before touching any schedule; review, ingestion and processing keep working. To pause publishing: set the variable and `tofu apply`; to stop faster, disable the EventBridge schedule (`publish_schedule_enabled = false`) or set the Lambda environment variable `PUBLISH_KILL_SWITCH=1` from the console. Resume by reverting the variable; parked schedules stay `PENDING` and are picked up when due.

## Changed residential/public IP

1. Find the new address (`curl -4 https://checkip.amazonaws.com` / `-6`).
2. Edit `review_ip_allowlist` in the tfvars (never in source) and `tofu apply`: the WAF IP sets and the Lambda variable are updated together.
3. Emergency without a workstation that can run OpenTofu: in the AWS console add the CIDR to the WAF IP set `<project>-owner-v4` **and** to the `REVIEW_IP_ALLOWLIST` variable of the console Lambda; record the change in the deployment report and reconcile the tfvars later. Never set `0.0.0.0/0`.

## Dead-letter queue redrive

Alarm `<project>-ingest-dlq-not-empty` fires when an ingest record failed three times. Inspect messages (`aws sqs receive-message --queue-url <dlq>`); a poisoned READY (e.g. a bucket policy change) is fixed at the source. Redrive with `aws sqs start-message-move-task --source-arn <dlq-arn> --destination-arn <queue-arn>` after confirming the cause; the ingestor is idempotent, so a redriven duplicate creates no second job.

## Processing task failed

The state machine marks the job `FAILED` (`WORKER_FAILED`) and the alarm `<project>-processing-failed` fires. Read the ECS log stream, fix the cause, then from the console use *Redrive* (reason + scope): this is an authenticated `reject` that creates the next revision.

## OAuth revocation / AUTH_REQUIRED

A schedule parked as `AUTH_REQUIRED` means the refresh token was revoked or expired. Run `python scripts/authorize.py` on the owner workstation (one-time consent), put the resulting JSON into the Secrets Manager secret `youtube-publisher` (`aws secretsmanager put-secret-value`), then set the schedule back to `PENDING`. No automatic retry happens.

## Quota exhaustion

`QUOTA_EXHAUSTED` parks the schedule; the daily quota resets at midnight Pacific time. Do not raise `daily_upload_cap` to compensate. Set the schedule back to `PENDING` the next day.

## Duplicate upload suspicion

`autopublisher.publishing.find_duplicate_uploads` lists assets with more than one video id. The system never deletes a remote video; the owner decides in YouTube Studio and records the decision in the audit log through a console action.

## Changing cutover_at

Only through the `cutover_at` variable with a written justification in `docs/deployment-report.md`; the value is also printed as a stack output. Lowering it can make old uploads eligible: it must never be lowered below the first production cutover.

## Backups and recovery

DynamoDB tables have point-in-time recovery; the bucket is versioned with non-current versions kept for 30 days. Recovery of a job: restore the tables to the point in time, and re-run the state machine for any revision whose `ready/` assets expired (the immutable source under `incoming/` is never expired by lifecycle).

## Cost guard

The AWS Budget `<project>-monthly` emails at 80 % actual and 100 % forecast. Fargate minutes and Bedrock tokens per revision are recorded in each revision's `cost` field for reconciliation.
