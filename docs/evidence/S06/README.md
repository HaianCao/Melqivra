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

## Second checkpoint: lifecycle identity and provisioning single-flight

The Colab runner now distinguishes a session that has never existed (`ABSENT`),
an in-flight allocation (`PROVISIONING`), an available session (`READY`), and a
session that was deliberately released (`RECLAIMED`). The public session name is
present only after the official CLI has confirmed an existing session or created
one successfully.

All concurrent `ensure_ready()` callers share one shielded provisioning task.
The regression test blocks the status probe, observes the public provisioning
state, adds a second caller, and proves the CLI receives exactly one status,
create, and Drive-mount sequence. Both callers observe the same completion, and
explicit stop transitions the runner to reclaimed with no published session ID.

This is a CPU-only contract test. It does not yet prove broker-restart recovery,
expired-auth handling, remote heartbeat, or a real concurrent T4 allocation.

## Third checkpoint: warm-session heartbeat and loss recovery

Every scheduler reuse now performs one official-CLI status heartbeat through the
same single-flight readiness task. If the named warm session disappeared outside
the control plane, its published identity is cleared, the state returns to
`PROVISIONING`, and exactly one replacement session is created before upload.
Concurrent callers share the heartbeat and recovery result rather than creating
multiple sessions.

The deterministic regression starts from a confirmed warm session, makes the
next heartbeat report it missing, and proves two concurrent readiness callers
produce one status probe and one replacement allocation. Real broker restart,
expired authentication, and post-allocation remote health still require later
failure injection and T4 evidence.

## Fourth checkpoint: ordered drain and reclaim

`stop()` is now serialized and cannot start a second official-CLI command beside
an active execution. A busy runner enters `DRAINING`, marks its active job for
cancellation, terminates the tracked command, and waits under a bounded timeout
for the run path to finish. Only then does it enter `STOPPING`, release the named
session, clear its public identity, and settle in `RECLAIMED`. The run cleanup is
state-aware and cannot overwrite draining or stopping with a stale `READY`.

The deterministic test pauses both cancellation and session release to observe
the ordered states and proves the active run ends as cancellation before reclaim.
This test does not yet provide remote cooperative cancellation markers inside the
worker or real hung-process cleanup evidence.

## Fifth checkpoint: pinned CLI and classified failures

The runtime and doctor now require the exact official `google-colab-cli` 0.8.0
contract verified in S01. Unparseable output or another release fails before any
session lookup or allocation. Nonzero status output is classified as expired
authentication, incompatible CLI behavior, a missing named session, or an
unclassified command failure. Only an explicit missing-session result may enter
`PROVISIONING`; auth, compatibility, and unknown failures remain `FAILED` and do
not attempt `new`.

CPU-only failure injection covers the accepted version, rejected version,
unrecognized version output, expired OAuth, missing session, and incompatible
command cases. Real expired-account behavior still needs a disposable or safely
revoked credential test; no provider session is created by this checkpoint.

## Sixth checkpoint: primary Modal readiness parity

The user explicitly expanded S06 hardening to the primary Modal provider. Modal
now pins the S01-verified CLI 1.6.3 contract in runtime configuration and doctor,
classifies authentication, compatibility, missing-app, and unknown command
failures, and performs a token heartbeat before every job reuse. Concurrent
readiness callers share one shielded task, so version and authentication checks
cannot fan out into duplicate control-plane operations.

Modal retains its scale-to-zero execution model: a readiness heartbeat validates
the local CLI and account rather than preserving a warm named GPU session. CPU
failure injection proves exact-version acceptance, mismatch rejection, expired
token rejection before submission, single-flight startup, and repeated heartbeat
behavior. No Modal app, container, or GPU is created by this checkpoint.

## Seventh checkpoint: ordered Modal drain and reclaim

Modal shutdown now follows the same authoritative lifecycle ordering as Colab.
A busy runner records its active job, enters `DRAINING`, marks that job cancelled,
terminates the tracked CLI process, and waits under the configured command bound
for run cleanup. It then enters `STOPPING`, stops only the scoped application, and
settles in `RECLAIMED`. Run cleanup clears active ownership and cannot overwrite
draining or stopping with stale `READY`.

The cancellation mapping now surrounds artifact transfer as well as remote
execution, so interruption after generation cannot be misreported as an ordinary
failure. The CPU regression pauses cancellation and app stop independently to
observe the complete state order. Real hung-container cleanup remains part of the
bounded provider evidence rather than this no-network checkpoint.

A scheduler integration regression also cancels a running Modal job through the
public harness boundary and verifies the durable record reaches `CANCELLED` with
the explicit `Job cancelled` reason rather than `FAILED`.

## Eighth checkpoint: idle boundary and broker restart matrix

Deterministic fake-clock integration now covers arrivals at 0, 299, exactly 300,
and 301 seconds. Work queued before the exact deadline recheck wins and reuses
the warm runner at 0/299/300; once the 300-second stop has committed, the
301-second arrival provisions a new runner. This preserves the product invariant
that queued work wins over idle shutdown without extending the grace period.

A separate crash simulation leaves a job durably `RUNNING`, closes the original
SQLite control plane, and starts a fresh harness on the same database. Startup
requeues the interrupted job, claims it under a new fencing token, and completes
it exactly once. These tests use FakeRunner and do not substitute for real Modal
or Colab reclaim timing evidence.
