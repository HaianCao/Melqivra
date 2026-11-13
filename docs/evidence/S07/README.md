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

Migration `0001_create_gpu_job_queue.sql` is packaged inside the wheel and applied under a
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
after the run; see `postgres-service-results.json`.

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

## Third checkpoint: expiring leases and global GPU capacity

Migration `0002_add_gpu_job_leases.sql` adds worker identity, expiry, and attempt count to
jobs plus one durable `gpu_capacity_slots` row. Claiming that row under
`FOR UPDATE` makes the reference deployment's concurrency-one limit global across
competing PostgreSQL workers rather than local to one scheduler process. Lease
decisions use PostgreSQL's clock after the capacity lock is acquired; SQLite reads
its clock after `BEGIN IMMEDIATE`. A delayed heartbeat therefore cannot revive a
lease that expired while waiting for the database lock.

Each harness has one stable worker ID. Its heartbeat covers provisioning, remote
execution, result processing, and the fenced terminal commit. Startup and the
scheduler loop recover only absent or expired leases, so a replacement process
does not steal live work. Lost renewal cancels the provider operation and cannot
write success, failure, or cancellation through the stale fencing token. Expired
cancel requests converge to `CANCELLED`; other expired attempts return to the FIFO
with a fresh fencing token and incremented attempt count. FIFO uses SQLite `rowid`
or PostgreSQL `enqueue_order`, not host wall clocks.

The SQLite failure suite proves an unexpired lease survives a broker restart, an
expired lease is reclaimed, a long fake job remains on its first renewed attempt,
and injected heartbeat loss cancels stale work before a second fenced attempt.
The disposable PostgreSQL 17.12 container passes eight real contracts, including
the version-1-to-version-2 migration, database-clock renewal/expiry, stale commit
rejection, cancelled-expiry convergence, and two workers competing for the single
capacity slot. PostgreSQL adapter branch coverage is 81% against its 80% gate.

The complete local gate discovers 224 tests: 216 pass and the eight PostgreSQL
tests skip without their explicit DSN. Strict mypy passes on 62 source files,
Ruff format/lint pass, critical scheduler/SQLite-store branch coverage is 84%,
both migrations are present in the wheel, distribution builds succeed, and the
dependency audit reports no known vulnerabilities. The disposable container and
its anonymous data volume were removed. Dead-letter policy, durable attempt
history, approvals/audit, authentication, SSE, quotas, S3/MinIO, and backup/restore
remain later S07 work.

## Fourth checkpoint: Python 3.11 cancellation ownership

The first hosted lease checkpoint passed PostgreSQL and every Python 3.13 job,
but Python 3.11 coverage exposed cancellation leakage from
`asyncio.wait_for(...)` in two scheduler paths. A timed-out wake wait could cancel
the scheduler's event future, while the provider timeout path did not explicitly
own the task it timed out. Both cases are now expressed as named tasks observed
with `asyncio.wait(...)`: the wake waiter persists across poll deadlines and is
cancelled only during orderly scheduler shutdown, and a provider timeout cancels
and awaits its owned task after requesting provider cancellation.

A regression test proves a timed-out provider operation is cancelled and stored
as `FAILED` rather than escaping or completing later. The complete local gate now
discovers 225 tests: 217 pass and the eight PostgreSQL tests skip without their
explicit DSN on both Python 3.13 and Python 3.11 coverage runs. Strict mypy passes
on 62 source files, Ruff format/lint pass, critical scheduler/SQLite-store branch
coverage remains 84%, distribution builds succeed, and the dependency audit
reports no known vulnerabilities. A fresh disposable PostgreSQL 17.12 container
again passed all eight contracts at 81% adapter branch coverage and was removed
with its anonymous volume.

Checkpoint `d82bf47` contains the compatibility fix and regression coverage.
Protected push run `43438710384` and pull-request run `43438720682` each passed
the four Windows/Linux Python 3.11/3.13 quality jobs plus the PostgreSQL service
contract on that exact commit.

## Fifth checkpoint: durable attempts and bounded dead-letter policy

Migration `0003_add_gpu_job_attempt_history.sql` expands the job state constraint with
`dead_lettered`, snapshots a positive `max_attempts` value on every accepted job,
and adds ordered fenced attempt rows. A live version-2 running attempt is
backfilled without changing ownership. Claim, heartbeat renewal, success,
failure, cancellation, lease expiry, and explicit interruption now update the
job and its attempt history in the same transaction.

Lease expiry requeues only while `attempt_count < max_attempts`. Cancellation
still converges to `CANCELLED`; the exhausted non-cancelled job converges to
`DEAD_LETTERED` with a finished timestamp and cannot be claimed again. Changing
the process default after enqueue does not alter a stored job's retry budget.
The HTTP job response exposes only attempt count and limit, not worker IDs,
fencing tokens, or lease timing.

The local suite discovers 229 tests: 219 pass and ten PostgreSQL tests skip
without their explicit DSN. Critical scheduler/SQLite-store branch coverage
remains 84%; strict mypy covers 62 source files, Ruff format/lint pass, the wheel
and source distribution build with migration 0003, and the dependency audit
reports no known vulnerabilities. A fresh PostgreSQL 17.12 database passes all
ten migration, parity, contention, attempt-history, dead-letter, and scheduler
contracts with 80% adapter branch coverage. The disposable container and its
anonymous volume were removed.

Checkpoint `79db7ed` contains the durable attempt history and bounded retry
policy. Protected push run `43443842155` and pull-request run `43443849360`
each passed the four Windows/Linux Python 3.11/3.13 quality jobs plus the real
PostgreSQL service contract on that exact commit.

The subsequent evidence-only push run `43444235862` exposed a scheduler-test
timing race on Ubuntu Python 3.11: the fixture reported one injected renewal
loss but left a 500-millisecond wall-clock lease, so a heavily delayed event
loop could legitimately expire the retry and produce a third fenced attempt.
Checkpoint `680a179` makes the injected first lease expire atomically, gives the
non-injected retry a ten-second margin, and asserts the exact durable outcomes
`lease_expired` then `succeeded`. Protected push run `43445175514` and
pull-request run `43445185089` each passed all five jobs on that exact fix.

## Sixth checkpoint: PostgreSQL project and revision authority

Project, archive, and creative-tool consumers now depend on the typed
`ProjectRepository` port instead of the SQLite implementation. SQLite remains the
development adapter. The new `PostgresProjectRepository` uses the same tracked
migrator and preserves tenant-scoped non-disclosure, immutable revision payloads,
checksum validation, stable lineage ordering, optimistic working pointers, and
accepted-pointer semantics.

Migration `0004_create_project_revision_store.sql` adds tenant-owned projects, immutable
checksummed revisions, same-project/document parent constraints, and working and
accepted pointers whose foreign keys cannot target another lineage. Project-row
locking serializes competing writers even when the first revision has no index
row yet; exactly one writer with a shared expected parent can commit.

A fresh PostgreSQL 17.12 database passed all fifteen job and project contracts.
The five project-specific tests cover matching SQLite ancestry/acceptance,
cross-tenant reads and mutation attempts, concurrent optimistic writers,
database-level immutability, and checksum corruption detection. Combined
PostgreSQL adapter branch coverage is 80% against the dedicated 80% gate. The
container and its anonymous volume were verified removed.

The complete local gate discovers 234 tests: 219 pass and fifteen PostgreSQL
tests skip without their explicit DSN. Strict mypy covers 63 source files, Ruff
format/lint pass, critical scheduler/SQLite-store branch coverage remains 84%,
the adapter and migration are present in the wheel, distribution builds succeed,
and the dependency audit reports no known vulnerabilities. Durable approvals,
tool audit records, authentication principals, request IDs, and service wiring
remain subsequent S07 work.

Checkpoint `050f06d` contains the repository port, PostgreSQL adapter, migration,
real-container contracts, CI expansion, and evidence. Protected push run
`43447676604` and pull-request run `43447688283` each passed the four
Windows/Linux Python 3.11/3.13 quality jobs plus the combined PostgreSQL job and
project service contract on that exact commit.

## Seventh checkpoint: persistence naming and migration catalog hardening

A repository-wide persistence review found that the behavior was sound but the
module/resource vocabulary had drifted. Generic `store.py` and
`postgres_store.py` names obscured their job-specific role, project adapters mixed
"store" and "repository", and the shared PostgreSQL migrator lived inside the job
adapter. The refactor now uses explicit `job_store`, `project_repository`, and
`postgres_migrations` modules throughout source, tests, scripts, coverage, and CI.

The four existing SQL resources retain their exact versions, logical migration
names, normalized SQL, and canonical SHA-256 hashes. Their packaged filenames are
now action-oriented:

- `0001_create_gpu_job_queue.sql`
- `0002_add_gpu_job_leases.sql`
- `0003_add_gpu_job_attempt_history.sql`
- `0004_create_project_revision_store.sql`

`docs/development/database-migrations.md` defines the append-only naming and
compatibility policy. CPU-safe catalog tests reject gaps, duplicate logical names
or resources, ambiguous filenames, missing wheel resources, and checksum drift.
Migration discovery/execution is isolated from both PostgreSQL adapters. The
legacy hosted check display name remains unchanged because branch protection
requires that exact external status; its command runs migrations plus both job and
project adapters.

The complete local gate discovers 236 tests: 221 pass and fifteen PostgreSQL
tests skip without an explicit DSN. Strict mypy covers 64 source files, Ruff
format/lint pass, critical scheduler/job-store branch coverage remains 84%,
distribution builds contain only the explicit new module/resource names, and the
dependency audit reports no known vulnerabilities. A fresh PostgreSQL 17.12
database again passes all fifteen real contracts at 80% combined migration and
adapter branch coverage; the container and anonymous volume were removed.
