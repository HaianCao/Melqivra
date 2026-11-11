# S01 evidence — T4 benchmark and backend decision

Status: `IN_PROGRESS`

Recorded: 2026-11-11 (Asia/Saigon)

## Candidate provenance established

- ACE-Step 1.5 release `v0.1.8` is pinned to source commit
  `dce621408bee8c31b4fcf4811682eb9359e1bc94`.
- The candidate turbo model bundle is pinned to Hugging Face revision
  `19671f406d603126926c1b7e2adc169acbcade22`.
- The candidate 0.6B LM is pinned to Hugging Face revision
  `148d8ea0225bdab342ee1ae3a354275ccd60ca80`.
- Large weight file sizes and SHA-256 values were read from Hugging Face
  immutable-revision response metadata without downloading the assets.
- Upstream source and model cards declare MIT. Transitive Qwen3-derived terms
  remain an explicit license-review item; no weights are bundled.

The machine-readable inventory is in
[model-registry.json](model-registry.json). Public license assumptions and open
review items are recorded in [MODEL_LICENSES.md](../../../MODEL_LICENSES.md).

## Prior notebook boundary

The ignored local `poc.ipynb` demonstrates that a Tesla T4 generated a
140-second sample, but it is not closure evidence because it cloned an unpinned
branch, patched dependency source in place, used a private uploaded reference,
embedded large outputs, and logged `llm_initialized=False` / `use_lm=False`.
S01 must reproduce results from clean, pinned inputs without silent capability
fallback.

## Benchmark contracts

- [benchmark-run.schema.json](benchmark-run.schema.json) defines the required
  environment, asset, capability, timing, memory, audio-validation, and artifact
  evidence for each run.
- [vietnamese-briefs.json](vietnamese-briefs.json) contains 20 project-authored
  legal fixtures spanning regions, registers, genres, tempos, structures, and
  code-switching.
- The initial baseline is 2B turbo, eight diffusion steps, shift 3.0, batch one,
  WAV master output, and an explicit LM-off versus 0.6B-LM-on comparison.

## Current environment blocker

The supported WSL2 distribution `Hami-Ubuntu` is installed, but currently has
no `uv`, `pip`, `pipx`, Colab CLI, or Colab session configuration. No T4 was
provisioned and no quota or paid compute was consumed during this checkpoint.
An interactive Colab login and confirmation to use the account's T4 quota are
required before the real benchmark can begin.

## Next evidence

1. Bootstrap the official Colab CLI in WSL2 and complete user-controlled login.
2. Verify T4 entitlement, CLI version, and a clean named-session healthcheck.
3. Build an idempotent pinned runtime/cache manifest without patching installed
   upstream source.
4. Run fresh and warm text-to-music baselines with LM off and LM 0.6B on.
5. Validate audio duration, sample rate, silence, clipping, NaN/Inf, checksums,
   peak VRAM, timing, and requested capability health.
6. Exercise reference, repaint, and extend only when upstream reports those
   capabilities explicitly; unsupported operations must fail clearly.
7. Complete license review and accept the backend ADR before closing S01.
