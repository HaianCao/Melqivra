# S06 evidence — Colab lifecycle and cache hardening

Status: `IN_PROGRESS`

Recorded from: 2026-11-13 (Asia/Saigon)

## Entry audit

The inherited Colab path already provides a named official-CLI T4 session,
on-demand provisioning, optional Drive mount, file-backed upload/execute/download,
one scheduler execution slot, durable job fencing, and a 300-second default idle
timeout. S01 separately proved a real T4 bootstrap, exact runtime, atomic Drive
cache population, cold restore, stale/corrupt rejection, and explicit shutdown.

The S06 audit identified remaining lifecycle gaps before closure:

- the public runner state does not yet distinguish absent, provisioning, ready,
  busy, draining, stopping, reclaimed, and failed;
- provisioning is not yet a tested single-flight operation across concurrent
  callers or control-plane restart;
- heartbeat, lost-session recovery, remote cancellation markers, and hung-worker
  cleanup are incomplete;
- the product Colab runner does not yet integrate the versioned Drive cache
  manifest or checksum artifact transfer;
- cold/warm/load/inference/transfer/idle/recovery metrics are not yet emitted as
  one lifecycle record;
- exact 0/299/300/301-second arrivals, broker restart, expired auth, CLI
  incompatibility, corrupt cache, and reclaim-during-load failure injection still
  require dedicated tests and real T4 evidence.

## First checkpoint: active CLI cancellation

The first S06 slice closes a concrete cost and correctness gap. A running Colab
job previously recorded a local cancellation request but left the active official
CLI process running, allowing remote work to continue and potentially surface as
a failed job.

`ColabCliClient` now tracks only its current allowlisted subprocess, interrupts it
cooperatively, escalates to terminate/kill under bounded waits, and cleans stream
reader tasks on timeout or task cancellation. `ColabRunner.cancel()` terminates
that active command and maps the expected nonzero CLI exit back to
`asyncio.CancelledError`. The scheduler regression proves the durable job reaches
`CANCELLED` with the explicit `Job cancelled` reason rather than `FAILED`.

This checkpoint allocates no provider session or GPU and does not claim remote
worker cooperation after the local CLI process has exited; that later cleanup and
reconciliation behavior remains part of S06.
