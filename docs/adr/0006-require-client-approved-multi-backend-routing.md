# ADR-0006: Require client-approved multi-backend routing

- Status: Accepted
- Date: 2026-11-13
- Owners: maintainers
- Supersedes: none

## Context

No single open backend currently satisfies every Melqivra music request on a
Tesla T4. HeartMuLa produced two valid real vocal takes on T4 and declares
Chinese, English, Japanese, Korean, and Spanish support, but it does not declare
Vietnamese or instrumental support and its measured VRAM margin is narrow. The
pinned ACE-Step revision produced valid 30-second instrumental audio, while its
vocal path failed the clean pinned contract. An older Colab proof of concept did
produce Vietnamese reference-conditioned vocals, but it used an unpinned clone,
an in-place float32 patch, and private reference audio.

Backend choice changes capability, cost, license, reproducibility, and failure
risk. The planner may analyze the user's intent, but it is not authorized to
silently choose an expensive or materially different execution path.

## Decision

Melqivra maintains an explicit catalog of exact backend revisions and verified
capabilities. The agent filters that catalog using the approved operation,
vocal or instrumental intent, language, and duration, then presents all
compatible options with their maturity and limitations. It may recommend an
option, but the client must approve the exact backend revision and generation
contract before any GPU job is scheduled.

The initial routing policy is:

- HeartMuLa is a verified S05 candidate for vocals only in its five declared
  languages (`zh`, `en`, `ja`, `ko`, and `es`). Its T4 memory warning remains
  visible.
- The clean pinned ACE-Step backend is research-only and routable only for the
  exact verified 30-second instrumental contract.
- Vietnamese ACE-Step reference-conditioned vocals remain an experimental
  capability with no product route until the pinned float32 fork passes a
  separately approved real-T4 experiment.
- Unsupported requests return no compatible option. Melqivra never changes
  backend, provider, GPU, precision, language, vocal intent, duration, or
  operation automatically.
- New backends enter the catalog only after source/model/license pins, bounded
  capability tests, real target-hardware evidence, audio validation, and
  artifact provenance are recorded.

Voice identity and cloning are not backend-routing shortcuts. They remain
default-off S12 research and require consent, verification, revocation,
deletion, abuse handling, leakage evaluation, and a kill switch.

## Alternatives considered

- Make HeartMuLa the universal default: rejected because its declared language
  and instrumental coverage is incomplete and its T4 margin is narrow.
- Automatically fall back to ACE-Step or another provider: rejected because it
  changes cost and semantics without client approval.
- Let the language model name any installed backend: rejected because model
  availability is not capability or license evidence.
- Block all S05 work until one universal model exists: rejected because explicit
  capability routing allows useful, honest vertical slices now.

## Consequences

Planning can produce zero, one, or several backend options. A zero-option result
is an actionable unsupported-capability response, not a generation failure.
Backend-specific adapters remain isolated behind the shared generation and job
contracts. Product surfaces must show the chosen backend and limitations in the
approval view and manifest.

## Validation

- `tests/test_backend_routing.py` proves language, vocal/instrumental, duration,
  maturity ordering, and no-fallback behavior.
- `docs/evidence/S05/heartmula-t4-run.json` records the successful HeartMuLa T4
  batch and measured memory margin.
- `docs/evidence/S01/` records the exact ACE-Step instrumental and vocal results.
- `docs/evidence/S05/ace-step-colab-poc-contract.json` records the privacy-safe
  reconstruction of the older Vietnamese-vocal proof of concept.

