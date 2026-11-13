# S07 evidence — durable multi-user service boundary

Status: `IN_PROGRESS`

Recorded from: 2026-11-13 (Asia/Saigon)

## Entry audit

S06 closed at `435ae63` with annotated tag
`stage-s06-gpu-provider-lifecycle`. The inherited service has strong single-node
job semantics—FIFO, per-user idempotency, fencing, cancellation, restart
requeue, atomic result publication, and object-level checks in the project
domain—but it is not yet the S07 production topology.

The S07 audit found these remaining boundaries:

- the scheduler is typed directly against `SqliteJobStore`, while project and
  artifact consumers also name concrete SQLite/filesystem adapters;
- jobs and projects live in separate local SQLite databases with no PostgreSQL
  migrations for users, approvals, attempts, leases, audit, or artifact metadata;
- restart recovery requeues every `RUNNING` job rather than reconciling an
  expiring heartbeat lease and dead-letter policy;
- local filesystem artifacts have atomic checksum semantics but no S3/MinIO
  adapter, publication record, retention policy, or orphan reconciliation;
- the HTTP boundary trusts development `X-User-Id`, has no authentication port,
  request ID/error contract, pagination, quota, or SSE reconnect protocol;
- backup/restore and multi-process API/worker recovery have no production
  evidence yet.

ADR-0007 establishes PostgreSQL as the durable metadata/job authority, limits
Redis to recoverable coordination, and places immutable bytes behind an
authorized S3-compatible object-store port. The first implementation checkpoint
will introduce the job-store port and tenant-scoped job lookup/cancellation
without changing the safe SQLite development default.
