# ADR-0004: Keep pinned ACE-Step research-only on T4

- Status: Accepted
- Date: 2026-11-12
- Owners: maintainers
- Supersedes: none

## Context

S01 evaluated ACE-Step 1.5 release `v0.1.8` at source revision
`dce621408bee8c31b4fcf4811682eb9359e1bc94` with immutable model revisions on
real Tesla T4 runtimes. Instrumental text-to-music generation produced valid,
reproducible WAV artifacts with substantial VRAM margin. The vocal path did not
meet the same contract: non-empty lyrics deterministically produced all-NaN
float16 latents, the advertised float32 override was not wired in the pinned
source, and two isolated 0.6B LM probes returned no metadata or audio codes.

A reviewed community fork wires float32 selection, but subsequent Modal T4 and
L4 calls received no container, so that fork has no execution evidence. Its
behavior cannot be substituted for the pinned upstream candidate.

## Decision

The pinned ACE-Step candidate is rejected as Melqivra's default production
music-generation backend. It remains an explicitly research-only backend for
the verified instrumental benchmark and for future isolated evaluation.

Melqivra must not advertise or silently attempt vocal generation, LM planning,
reference conditioning, repaint, extend, or continuation through this candidate
unless a separately pinned revision passes the corresponding real-GPU contract.
Those capabilities are unsupported by default and must fail before GPU work is
scheduled.

Colab remains a viable on-demand runner transport because its T4 lifecycle,
cache restore, inference, validation, and shutdown paths were demonstrated.
Modal remains an optional research execution provider with scale-to-zero and a
persistent cache, not a product dependency or automatic fallback.

No model weights are bundled. Users or operators retrieve exact upstream
revisions into their own cache. Releases preserve ACE-Step MIT attribution and
Qwen3 Apache-2.0 attribution and provenance. A future decision to redistribute
weights requires a new legal review and ADR.

## Alternatives considered

- Accept the candidate for instrumental and vocal use: rejected because the
  vocal and LM contracts fail deterministically on the reference T4.
- Patch the pinned source at runtime: rejected because it breaks the clean,
  reproducible source pin and hides a materially different backend.
- Adopt the community float32 fork immediately: rejected because it has not
  received a GPU container in this environment and therefore has no generation,
  memory, timing, or artifact evidence.
- Upgrade the reference GPU: rejected as the default because the product target
  and user direction retain T4 compatibility.

## Consequences

S02 through S04 may proceed without a production audio backend because their
domain, agent, and creative-planning contracts are GPU-independent. S05 cannot
claim a real audio vertical slice until a new backend or revision is separately
pinned, licensed, benchmarked, and accepted.

The product capability matrix is intentionally conservative. Instrumental
research evidence is retained, but unsupported operations fail explicitly and
never degrade to full-song regeneration, another precision, another model, or
another provider.

## Validation

- `docs/evidence/S01/` preserves real T4 timings, VRAM, audio validation,
  checksums, cache behavior, numerical failures, LM contract failures, and
  provider-allocation evidence.
- `MODEL_LICENSES.md` and `license-review.json` record the non-bundling policy,
  immutable license evidence, and required attribution boundary.
- Future backend acceptance requires a new ADR with successful real-GPU
  capability evidence; FakeRunner or provider initialization alone is
  insufficient.
