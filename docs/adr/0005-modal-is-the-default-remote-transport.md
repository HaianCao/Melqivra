# ADR-0005: Use Modal as the default remote execution transport

- Status: Accepted
- Date: 2026-11-12
- Owners: maintainers
- Supersedes: ADR-0004's statement that Modal is research-only as a transport

## Context

Melqivra needs one durable execution boundary whose provider can be changed
without changing scheduler, project, approval, or artifact semantics. Colab T4
proved the original pinned research path, while Modal later reproduced the same
instrumental contract and provided scale-to-zero execution. The prior code chose
runners inside the scheduler, and the prior ADR described Modal only as a
research provider.

Execution transport and music-generation backend acceptance are separate
decisions. Modal transport success does not make the pinned ACE-Step candidate a
production backend; ADR-0004's model and capability restrictions remain in
force.

## Decision

`GPU_RUNNER` selects an explicit runner strategy at application startup. The
registered strategies are `modal`, `colab`, `fake`, and `local`; `modal` is the
application default. Unknown, unavailable, timed-out, or unsupported strategies
fail explicitly. Melqivra never changes provider, GPU type, model, precision, or
operation automatically.

Modal jobs cross the same durable concurrency-one job boundary as every other
provider. Control-plane health and transport checks use a scale-to-zero CPU
function. GPU job kinds use a T4 function with `max_containers=1` and an
immediate scale-down window. Private request content is passed through temporary
files rather than CLI arguments. Modal and Colab execute the same packaged,
allowlisted worker contract.

The installable wheel contains the Modal app and remote worker assets. Docker
and CI select `fake` explicitly so credential-free development remains
reproducible. Colab remains a supported, explicitly selectable alternative and
retains its named-session warm-idle behavior when selected.

Choosing Modal as the default transport does not accept a
`MusicGenerationBackend`. S05 must separately pin, license, benchmark, and
accept a backend before `music.generate` can succeed.

## Alternatives considered

- Keep provider conditionals in the scheduler: rejected because lifecycle
  construction would continue leaking provider details into orchestration.
- Automatically fall back from Modal to Colab: rejected because it changes
  cost, credentials, environment, and reproducibility without user approval.
- Keep FakeRunner as the application default: rejected by the product direction
  to make the verified on-demand remote path the normal configured strategy;
  Docker and CI still select the fake explicitly.
- Treat the successful ACE-Step instrumental reproduction as backend acceptance:
  rejected because vocal, LM-sensitive, reference, repaint, extend, and OOM
  contracts remain unresolved or unsupported.

## Consequences

Default host/WSL operation requires the official Modal CLI and authentication.
Provider capacity failures are visible durable job failures rather than hidden
fallbacks. Operators can switch to Colab with one configuration value without
changing domain or scheduler code.

Modal scale-to-zero does not provide Colab-style warm model residency. S05 and
S06 must measure backend-specific cache, load, transfer, cancellation, and
recovery behavior instead of assuming the two providers have identical
lifecycle economics.

## Validation

- Strategy registry and provider contract tests cover explicit selection,
  unknown-provider rejection, file transport, cancellation, and cleanup.
- A real Modal healthcheck crossed the SQLite queue, strategy registry, official
  CLI, remote function, packaged worker, and fenced completion boundary.
- The successful Modal T4 `vi-014` reproduction remains the audio-level
  transport proof; the default product worker still rejects generation because
  no production backend plugin is installed.
- Wheel inspection and an isolated wheel install prove both remote assets are
  present and selected by default without a source checkout.

