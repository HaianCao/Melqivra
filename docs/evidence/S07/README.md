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

## Eighth checkpoint: deterministic restart lease evidence

The professionalization checkpoint passed nine of ten protected jobs, while one
duplicate Windows/Python 3.13 coverage run exposed a timing assumption in the
broker-restart test. The test assigned the crashed worker a 300-millisecond lease
and then assumed a 30-millisecond coroutine delay would observe it before expiry.
Under a loaded hosted runner the coroutine resumed after roughly one second, so
the service correctly reclaimed the expired lease and the test incorrectly
reported a failure.

`SqliteJobStore` now accepts an optional wall-clock dependency while retaining
the existing dynamic system-clock default. The restart test uses that clock to
prove the old worker remains authoritative before expiry, advances to the exact
expiry boundary, and then proves recovery completes with a new fencing token and
second durable attempt. The regression passed 25 consecutive runs. The complete
local gate again discovers 236 tests: 221 pass and fifteen PostgreSQL tests skip;
strict mypy covers 64 source files, Ruff format/lint pass, critical
scheduler/job-store branch coverage is 84%, distributions build successfully,
and the dependency audit reports no known vulnerabilities.

Checkpoint `93ffaa7` contains the deterministic restart-lease regression fix.
Protected push run `43455902574` and pull-request run `43455911329` each passed
all five jobs on that exact commit.

## Ninth checkpoint: durable approvals and tool execution audit

The process-local approval ledger is replaced by the typed `ApprovalRepository`
port and thread-safe `InMemoryApprovalRepository` development adapter.
`PostgresApprovalRepository` persists the exact user, project, action, and risk
binding. A decision is one-way: only `pending` may transition, exactly once, to
`approved` or `rejected`; the database rejects binding mutation and deletion.
Cross-tenant lookup and decision attempts remain indistinguishable from a
missing approval.

Tool idempotency and audit now cross the `ToolExecutionRepository` port.
`PostgresToolExecutionRepository` atomically reserves a tenant/project/tool-call
ID before handler execution. A completed call stores its canonical result for
restart-safe replay, changed input produces an explicit idempotency conflict,
and a concurrent duplicate observes `in_progress` instead of invoking the
handler. The success result and audit event commit in one PostgreSQL transaction.
Audit rows are append-only and contain IDs, actor, tool name, outcome, turn, and
optional result revision only; arguments and result content are absent.

Migration `0005_create_approval_audit_authority.sql` adds the approval,
tool-execution, and audit authority with tenant/project foreign keys and database
transition triggers. A fresh PostgreSQL 17.12 database passes twenty job,
project, approval, replay, audit, and contention contracts at 81% combined
adapter branch coverage. The policy-specific tests prove approval restart,
one-way decisions, changed-contract and cross-tenant rejection, audit
immutability, handler-free replay after repository restart, and exactly one
winner across competing reservations. The disposable container and anonymous
volume were removed.

The complete local gate discovers 245 tests: 225 pass and twenty PostgreSQL
tests skip without an explicit DSN. Strict mypy covers 68 source files, Ruff
format/lint pass, critical scheduler/job-store branch coverage remains 84%, the
new migration and adapters are present in the wheel, distribution builds
succeed, and the dependency audit reports no known vulnerabilities. Production
principal authentication, request/error contracts, SSE reconnect, quotas,
S3-compatible artifacts, and backup/restore remain later S07 work.

Checkpoint `b4ef4b7` contains the durable approval, tool replay, and append-only
audit authority. Protected push run `43459137014` and pull-request run
`43459144565` each passed all five jobs on that exact commit.

## Tenth checkpoint: authenticated, bounded API contract

The HTTP boundary now resolves a typed `Principal` through an injected
`Authenticator`; endpoints no longer read identity headers directly.
`DevelopmentHeaderAuthenticator` keeps the credential-free local workflow
explicit, while `API_AUTH_MODE=injected` refuses application construction unless
the deployment supplies an authenticator. The included static bearer adapter
uses constant-time credential comparison and ignores caller-supplied identity
headers. Generated OpenAPI declares the active development-header or bearer
security scheme.

Every public error now uses a stable code/message/request-ID envelope. Safe
client request IDs are propagated; unsafe values are replaced. Responses include
the request ID, `nosniff`, and `no-store` headers. Validation, authentication,
unknown-route, missing-job, payload-limit, quota, cursor, and page-size failures
have deterministic contracts without lease owners, fencing tokens, credentials,
or framework exception text.

Tenant job listing uses stable FIFO cursor pagination in both SQLite and
PostgreSQL. A missing or cross-tenant cursor has the same invalid-cursor result.
Payload and page sizes are bounded. Active-job quotas are enforced inside the
enqueue transaction: SQLite uses its immediate writer lock and PostgreSQL uses a
tenant-keyed advisory transaction lock. An idempotent replay is resolved before
the quota count, so a network retry returns the original job even at the limit.
A real PostgreSQL contention test proves two concurrent same-tenant enqueues for
one remaining slot produce exactly one accepted job and one quota rejection.

The complete local gate discovers 255 tests: 233 pass and twenty-two PostgreSQL
tests skip without an explicit DSN. Strict mypy covers 69 source files, Ruff
format/lint pass, critical scheduler/job-store branch coverage is 86%,
distribution builds succeed, and the dependency audit reports no known
vulnerabilities. A fresh PostgreSQL 17.12 database passes all twenty-two real
contracts at 81% combined adapter coverage; the disposable container and volume
were removed. Durable SSE reconnect, production artifact storage,
backup/restore, retention, and broader rate/token quotas remain later S07 work.

The first protected run of this checkpoint exposed a Python 3.11 compatibility
defect in Starlette's task-group-based `BaseHTTPMiddleware` path. Functional API
assertions passed on Python 3.13, but on Python 3.11 the middleware could cancel
the request-body reader and surface `RuntimeError: No response returned`. The
request contract now uses a pure ASGI wrapper that assigns request identity in
the scope and appends response headers directly, without a secondary task group.
The API suite passes repeatedly in an isolated Python 3.11 environment, and the
complete local gate again passes all 255 discovered tests with the same typing,
coverage, build, and audit results.

Checkpoint `3a6e983` contains that pure-ASGI compatibility fix. Protected push
run `43465738875` and pull-request run `43465749057` each pass the four
Windows/Linux Python 3.11/3.13 jobs plus the real PostgreSQL contract.

## Eleventh checkpoint: durable job events and SSE reconnect

Migration `0006_create_gpu_job_event_stream.sql` adds an ordered event stream
whose sequence is allocated under the owning job row. Existing version-5 jobs
receive one explicit `job.snapshot` backfill; new jobs record queue, claim,
provider progress, cancel request, terminal completion, lease recovery, retry,
and dead-letter transitions. Lifecycle state and its event commit in the same
SQLite or PostgreSQL transaction. Provider progress requires the current fencing
token, so a stale worker cannot append after lease loss; event types and JSON are
validated and each payload is capped at 16 KiB before durable insertion.

The typed job-store port exposes only tenant-scoped event reads. A caller cannot
distinguish a missing job from another tenant's job, and an event cursor beyond
the durable high-water mark is rejected. Concurrent progress writers serialize
to contiguous per-job sequences. Restarted SQLite and PostgreSQL adapters replay
the same persisted events without relying on Redis or process memory.

`GET /v1/gpu/jobs/{job_id}/events` returns authenticated server-sent events. Each
record uses its durable sequence as the SSE `id`; `Last-Event-ID` resumes strictly
after that sequence. The stream drains all committed events before closing after
a terminal state, emits bounded keepalive comments while active, disables proxy
buffering, and stops polling after client disconnect. The optional in-process
notification hook runs only after durable append and cannot fail the job.

A fresh PostgreSQL 17.12 container passed all twenty-four job, migration,
project, approval, replay, audit, contention, recovery, and event contracts at
81% combined adapter branch coverage. The event-specific contracts cover v5
backfill, concurrent ordering, stale fencing, restart replay, tenant isolation,
invalid cursors, terminal agreement, cancellation, lease-expiry requeue, and
dead-letter convergence. The disposable container and anonymous volume were
removed.

The complete local gate discovers 262 tests: 238 pass and twenty-four PostgreSQL
tests skip without an explicit DSN. Strict mypy covers 69 source files, Ruff
format/lint pass, critical scheduler/job-store branch coverage is 87%, migration
0006 is present in the wheel, distribution builds succeed, and the dependency
audit reports no known vulnerabilities. S3/MinIO publication, backup/restore,
retention/orphan reconciliation, broader quotas/observability, and the final
multi-process restart demo remain later S07 work.

Both protected Python 3.11 jobs then exposed a finite-stream compatibility defect
while the PostgreSQL contract and all Python 3.13 jobs passed. A terminal replay
still used Starlette's live `StreamingResponse`; its disconnect-listener task
group could cancel the normally completing response task under Python 3.11 and
surface `CancelledError` or AnyIO `InvalidStateError` after the complete event
payload had been produced.

Terminal jobs now return the same authenticated SSE representation as a finite
`Response`, without starting a disconnect-listener task. Active jobs retain the
polling `StreamingResponse`, keepalive, disconnect, and reconnect behavior. The
API suite passes on Python 3.11 and 3.13, and the complete 262-test gate passes on
both runtimes with Ruff, strict mypy, the 87% critical-path coverage gate,
distribution builds, and the dependency audit.

Checkpoint `2e137cf` contains the terminal-replay compatibility fix. Protected
push run `43475168987` and pull-request run `43475172586` each pass the four
Windows/Linux Python 3.11/3.13 jobs plus the real PostgreSQL contract.

## Twelfth checkpoint: PostgreSQL and S3 artifact publication authority

Migration `0007_create_artifact_publication_authority.sql` adds tenant/project
artifact batches and ordered members with immutable checksums, lengths, media
types, source revisions, internal object keys, and explicit publication state.
The database enforces project ownership, append-only contracts, legal state
transitions, and source-revision membership. A pending batch is not visible to
authorized reads; PostgreSQL exposes every member together only after the byte
contract has completed.

The provider-neutral `ObjectStore`, `ArtifactRepository`, `ArtifactPublisher`,
and `ArtifactReader` ports separate authority from transport. The optional boto3
adapter uses conditional immutable S3 writes, bounded standard retries and
timeouts, safe key validation, paginated listing, provider-error sanitization,
and explicit response-body closure. Every verification and authorized download
is capped by PostgreSQL's immutable recorded length: both an oversized provider
header and a body that exceeds a dishonest header fail as integrity errors after
the response is closed. The publication service reserves metadata, uploads each
object, downloads it back, independently verifies length and SHA-256, and only
then commits the batch as published. An interrupted upload leaves a restart-safe
pending record that remains invisible; an exact retry resumes safely, while
changed content conflicts.

`ARTIFACT_STORAGE=s3` is explicit and requires the PostgreSQL production job
authority. Configuration uses the standard AWS credential chain; browser and
job responses receive neither credentials nor internal object keys. The API
download route authorizes through PostgreSQL first, returns the same 404 for
missing and cross-tenant artifacts, rehashes downloaded bytes, and converts
missing/corrupt provider state to a content-free 503 response. Filesystem
`AtomicArtifactStore` remains the default development publisher.

A real PostgreSQL 17.12 plus S3-compatible run passed 28 contracts: all 27
repository/migration/job/project/policy contracts plus one end-to-end object
publication, restart-safe retry, authorized read, and remote-corruption test.
The S3 service used digest-pinned Alpine MinIO packaging at source revision
`951e146`, ran as non-root UID 100, and was used only as disposable AGPL-3.0 test
infrastructure; it is not bundled or selected as a production image. Both
containers and all disposable volumes were removed. Exact image, license,
cleanup, migration, and contract details are in `artifact-storage-results.json`.

The complete local gate discovers 276 tests: 248 pass and twenty-eight explicit
PostgreSQL/S3 tests skip without configured endpoints. Strict mypy covers 74
source files, Ruff format/lint pass, critical scheduler/job-store branch coverage
is 87%, the combined real PostgreSQL adapter gate is 80%, distribution builds
contain migration 0007 and every artifact module, and the dependency audit
reports no known vulnerabilities. Retention/deletion recovery, orphan
reconciliation, backup/restore, broader quotas/observability, and the final
multi-process restart demo remain later S07 work.

Checkpoint `0c17a32` contains the artifact metadata authority, object-store
adapter, authorized API read, bounded-download hardening, real-service evidence,
and dependency/CI updates. Protected push run `43485358189` and pull-request run
`43485366292` each pass the four Windows/Linux Python 3.11/3.13 jobs plus the
combined PostgreSQL and S3-compatible service contract on that exact commit.
