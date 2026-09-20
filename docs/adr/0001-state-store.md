# ADR 0001 — Job state in DynamoDB with a local JSON twin

Date: 2026-09-20. Status: accepted.

## Context

The repository already used DynamoDB for jobs. The specification requires immutable revisions, optimistic locking, an append-only audit trail and idempotent job creation under duplicate S3 events, plus a complete local development path with no AWS.

## Decision

Keep DynamoDB (five pay-per-request tables: jobs, revisions, schedules, publications, audit) and put every conditional rule in one `JobStore` interface: create-if-absent for jobs, revisions, schedules and publications; update-if-version for jobs, schedules and publications; review-only updates for revisions. A `LocalJobStore` stores the same documents as JSON files under a file lock with identical semantics, so the tests and the local stack exercise the same rules the cloud enforces.

## Alternatives rejected

- SQLite locally and DynamoDB in AWS with different code paths: two implementations of the conditional rules to keep in sync.
- A relational store in AWS (RDS/Aurora): ongoing cost for a single-user system; conditional writes are enough.
- Keeping the old single-table, six-status model: it could not express revisions, per-asset approval or locking.
