# ADR-0002: All GPU work uses the job boundary

- Status: Accepted
- Date: 2026-11-11
- Owners: maintainers
- Supersedes: none

## Context

Music generation is expensive, long-running, failure-prone, and limited to one
T4 in the reference deployment. Direct request-to-GPU calls cannot provide fair
queuing, cancellation, idempotency, restart recovery, or reliable shutdown.

## Decision

Every GPU action is represented by a durable job. The reference deployment has
global concurrency one. Jobs use idempotency keys and fenced attempts. Colab is
provisioned on demand, reused while work exists, and stopped after a configurable
idle period that defaults to 300 seconds. The scheduler rechecks queued and
running work immediately before stopping the runner.

## Alternatives considered

- Direct synchronous generation: simple but loses work on disconnect/restart.
- One Colab session per user: exceeds quota/cost and complicates credentials.
- Unlimited worker concurrency: incompatible with the reference T4 capacity.

## Consequences

Users may wait in a queue, so position and progress must be visible. The job
state machine becomes a critical consistency boundary and requires failure
injection, lease, fencing, cancellation, and recovery tests.

## Validation

S06 and S07 must prove serialization, boundary timing, multi-user queuing,
restart recovery, stale-worker rejection, cancellation, and idle shutdown.

