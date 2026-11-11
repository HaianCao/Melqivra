# ADR-0003: Use a capability-based runner abstraction

- Status: Accepted
- Date: 2026-11-11
- Owners: maintainers
- Supersedes: none

## Context

Developers need CPU-only tests, Colab T4 execution, and eventually local NVIDIA
GPU support. These environments differ in lifecycle and transport but must not
change project, job, approval, or artifact semantics.

## Decision

Fake, Colab, and local CUDA implementations conform to a typed runner contract.
Backends publish explicit capabilities; callers reject unsupported operations
instead of guessing or silently degrading. Domain and API layers depend on the
runner interface, not a concrete provider.

## Alternatives considered

- Provider-specific application flows: faster initially but duplicates logic.
- Pretend all backends support every operation: creates hidden full-regeneration
  and quality surprises.
- Runtime monkey-patching: weakens reproducibility and type safety.

## Consequences

Shared contract tests are required. Capability negotiation becomes part of the
generation plan and UI. Provider-specific lifecycle details stay behind adapters.

## Validation

FakeRunner covers deterministic failure tests. S05, S06, and S11 add shared
contract and manifest-parity tests for real backends.

