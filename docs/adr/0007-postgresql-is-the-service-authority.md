# ADR-0007: PostgreSQL is the durable service authority

- Status: Accepted
- Date: 2026-11-13
- Owners: maintainers
- Supersedes: none

## Context

The SQLite queue and project repository established correct single-control-plane
semantics, but S07 must support API and worker restarts, multiple users, leases,
fencing, audit, backup, and object-level authorization. Redis and S3-compatible
storage are useful production components, but allowing either to become an
implicit metadata authority would split state and make recovery ambiguous.

## Decision

PostgreSQL is the durable authority for users, projects, immutable revisions,
approvals, jobs, attempts, leases, artifact metadata, and audit records. State
transitions that grant ownership or publish success use PostgreSQL transactions
and fencing conditions.

Redis-compatible infrastructure may provide wakeups, ephemeral coordination,
rate-limit counters, and event fan-out. It is never the sole record of accepted
state, queue ownership, idempotency, or completion. Losing Redis must not lose a
job or authorize a stale worker.

S3-compatible storage, including MinIO, stores immutable artifact bytes.
PostgreSQL records tenant/project ownership, object key, media type, length,
checksum, lineage, and publication state. An artifact is visible only after its
metadata and object contract are complete; object lookup is always authorized
through tenant-scoped repository methods.

Application services depend on typed persistence and object-store ports rather
than SQLite, PostgreSQL, Redis, or an S3 SDK. SQLite and the local filesystem
remain supported development/reference adapters until the S07 production
adapters and migrations pass their shared contract suites.

## Alternatives considered

- Keep SQLite for production: simple, but unsuitable for multiple API/worker
  processes and production lease contention.
- Make Redis the queue authority: fast, but weakens durable audit, relational
  authorization, backup, and restore semantics.
- Store large artifact bytes in PostgreSQL: one authority, but inefficient for
  audio transfer and incompatible with the intended S3/MinIO deployment model.
- Let each adapter define its own state machine: expedient, but risks semantic
  drift and provider-specific recovery behavior.

## Consequences

S07 requires explicit migrations, transaction-bound claim/lease operations,
shared adapter contracts, restore evidence, and failure injection across API,
worker, database, coordination, and object storage. Production deployment gains
additional services and operational complexity. The domain and scheduler retain
the existing job/revision invariants through injected ports.

## Validation

S07 must prove upgrade and restore, duplicate delivery, lease expiry and stale
fencing, cross-tenant non-disclosure, cancellation at every state, API/worker
restart, S3 corruption/orphan reconciliation, and many-user serialization with
global GPU concurrency one.
