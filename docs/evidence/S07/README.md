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

## First checkpoint: job-store port and tenant-scoped access

Checkpoint `a3d8368` accepts ADR-0007 and records the entry audit. The scheduler
now depends on the typed `JobStore` port rather than `SqliteJobStore`, preserving
the current SQLite adapter while making the PostgreSQL job authority an explicit
replaceable boundary.

The port requires tenant-scoped lookup and cancellation operations. The SQLite
adapter performs those lookups with both job and user IDs, and the HTTP service
uses only the scoped harness methods for GET and DELETE. Missing and cross-tenant
jobs therefore return the same 404 body without loading another user's job into
the API authorization path. Direct store tests also prove an unauthorized cancel
does not change state before the owner performs the transition.

The complete checkpoint gate passes with 210 tests, strict mypy on 61 source
files, Ruff format/lint, 89% critical scheduler/store branch coverage,
successful wheel and source distribution builds, and no known audited
vulnerabilities. Development `X-User-Id` remains intentionally explicit; the
production authentication port and principal binding are subsequent S07 work.

## Second checkpoint: packaged PostgreSQL job adapter

The optional `postgres` installation extra now pins Psycopg 3.3.9 with its binary
and pool distributions. `dependency-review.json` records their LGPL-3.0-only
metadata, purpose, and alternatives; the locked dependency audit reports no known
vulnerabilities.

Migration `0001_job_queue.sql` is packaged inside the wheel and applied under a
PostgreSQL advisory transaction lock. The migration history stores a canonical,
line-ending-independent SHA-256 and rejects changed or unknown applied versions.
The first schema preserves strict FIFO through an identity enqueue order,
tenant-scoped idempotency, explicit states, cancellation, and a monotonic fencing
sequence.

`PostgresJobStore` implements the S07 port with pooled connections,
`FOR UPDATE SKIP LOCKED` claims, fenced completion, tenant-scoped lookup/cancel,
queue position, and restart requeue parity. `JOB_STORE=postgres` is explicit,
requires `POSTGRES_DSN`, never falls back to SQLite, and is checked by a sanitized
`melqivra doctor` probe. SQLite remains the safe default.

A disposable PostgreSQL 17.12 container from the exact CI image digest passed six
real contracts: idempotent/checksummed migration, changed-migration rejection,
strict FIFO and stale fencing, cross-tenant cancellation isolation, competing
worker claims, scheduler completion, and restart retrieval. PostgreSQL adapter
branch coverage is 81% with an 80% dedicated threshold. The container was removed
after the run; see `postgres-job-store-results.json`.

The standard local suite discovered 218 tests: 212 passed and the six PostgreSQL
tests skipped without their explicit DSN. Strict mypy passes on 62 source files,
Ruff format/lint pass, critical scheduler/SQLite-store branch coverage is 88%,
wheel and source distributions build with the SQL migration included, and the
dependency audit reports no known vulnerabilities. Expiring heartbeat leases,
attempt/dead-letter history, PostgreSQL project/revision/approval/audit schemas,
backup/restore, and multi-process failure recovery remain later S07 work.

Checkpoint `65873e2` contains the adapter, migration, configuration, dependency,
local-container evidence, and dedicated CI job. Checkpoint `d05a85f` corrects the
new job's immutable checkout-action SHA. Protected push run `43430479894` and PR
run `43430486001` each passed the four Windows/Linux Python 3.11/3.13 quality jobs
plus the real PostgreSQL service contract.
