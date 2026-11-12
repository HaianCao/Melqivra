# S04 evidence — Creative planning and lyrics workflow

Date: 2026-11-12

Status: `IN_PROGRESS` pending protected CI and the closure commit/tag.

Implementation checkpoint:
`a2a8857 feat(s04): add creative planning and lyrics workflow`.

## Delivered contract

- Rights-aware structured briefs covering concept, audience, language, genre,
  mood, tempo, duration, vocal direction, references, exclusions, style traits,
  and optional user lyrics.
- Deterministic duration-balanced blueprints with stable section IDs, energy,
  narrative role, instrumentation, delivery, transition, and exactly one
  explicit climax.
- Lyrics generation from planner candidates or authorized user text, plus
  continuation, restructuring, critique, and constrained rewriting.
- Stable line/section IDs and strict line/section lock invariance.
- Deterministic Vietnamese syllable-length, repeated-tone, rhyme, NFC Unicode,
  line-splitting, and mixed-language warnings. These are review aids, not claims
  of linguistic or musical quality.
- Named-identity style removal, explicit voice-imitation rejection, and lyrics
  rights required before transformations.
- Capability-aware plan compilation that rejects unsupported operations,
  languages, vocals, and durations without fallback; every plan specifies 2–4
  labeled variations with explicit seeds.
- Human-readable approval views and checksummed accepted contracts that pin the
  exact immutable brief, blueprint, lyrics, and plan revisions.
- Authenticated S03 tool schemas for every creative proposal and review action.
  These tools are pure: they do not commit state or start a runner.

## Verification

The complete local gate passed:

```text
uv sync --locked --extra dev                         PASS
ruff format --check src tests scripts                PASS
ruff check src tests scripts                         PASS
mypy                                                  PASS (27 source files)
coverage run -m unittest discover -s tests -v         PASS (95 tests)
coverage report                                       PASS (81% critical paths)
uv build                                               PASS (wheel and sdist)
pip-audit                                              PASS (no known vulnerabilities)
```

Stage-specific evidence includes:

- Golden 180-second Vietnamese blueprint fixture.
- Idea-only and authorized user-lyrics workflows.
- Verse/line lock invariance while changing the climax or chorus.
- Duration mismatch and unsupported capability rejection.
- Unicode normalization and Vietnamese/English warning behavior.
- Identity imitation and unknown-rights rejection.
- Cross-tenant creative-tool denial and explicit JSON Schemas.
- Accepted-contract checksum stability after unrelated working edits.

## CPU-only demo

```shell
uv run --frozen python scripts/s04_creative_demo.py
```

The demo produces only aggregate synthetic-fixture metadata. It performs no
network request, provider call, model load, GPU allocation, or artifact write.

## Deliberate limitations

- Prosody warnings are deterministic heuristics for human review, not a native
  speaker quality score; evaluation belongs to S10.
- S04 compiles generation contracts but does not translate them to a music model
  or generate audio. That boundary begins in S05.
- Living-artist identity names are supplied by policy/configuration; generic
  direct-imitation and voice-cloning language is rejected conservatively.
