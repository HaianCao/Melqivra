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

## Colab T4 bootstrap and transport

The supported WSL2 distribution `Hami-Ubuntu` has Python 3.12.3, `uv` 0.13.7,
and the official `google-colab-cli` 0.8.0 installed for the unprivileged user.
OAuth now persists through the Secret Service keyring without a plaintext
fallback. A named `melqivra-t4` session provisioned a Tesla T4 with 15,360 MiB,
driver 580.82.07, and remote Python 3.13.16. The existing allowlisted worker
passed upload, execute, and download transport in 0.028 seconds.

The tracked [runtime bootstrap](runtime-bootstrap.json) then checked out exact
ACE-Step commit `dce6214`, created the upstream locked Python 3.12 environment,
and verified Torch 2.10.0+cu128 with CUDA 12.8 available. No credential, quota
balance, private path, or user content is recorded in evidence.

## Verified model cache

`scripts/colab/s01_runtime.py` downloads only the pinned turbo, VAE, embedding,
and 0.6B LM families, then rejects any registered size or SHA-256 mismatch. The
first local population verified 7.64 GB of registered large assets in 88.63
seconds. [cache-manifest.json](cache-manifest.json) records the sanitized result.

Google Drive authorization was completed separately from Colab CLI OAuth. The
same script then atomically published the verified 7.64 GB cache through a
hidden temporary directory in 174.38 seconds; end-to-end download, verification,
and persistence took 265.30 seconds. A fresh T4 with no local model files restored
the cache from Drive in 113.51 seconds and reverified every registered SHA-256;
the full restore operation took 277.39 seconds.

Temporary real-Drive probes also proved that a stale source revision and a
zero-byte model are rejected explicitly. The probes were removed in `finally`
and did not modify the authoritative cache. The sanitized record is
[drive-cache-evidence.json](drive-cache-evidence.json). The final T4 session was
explicitly stopped and the CLI reported no active sessions.

## First real T4 baselines

`scripts/colab/s01_benchmark.py` generated and validated a 30-second Vietnamese
instrumental fixture (`vi-014`) with the pinned turbo model, eight diffusion
steps, shift 3.0, batch one, seed 20261111, and WAV output.

| Run | Model load | Inference | Peak allocated VRAM | Result |
|---|---:|---:|---:|---|
| DiT-only, fresh local cache | 58.25 s | 6.62 s | 7.83 GB | passed |
| DiT-only, warm local cache | 27.66 s | 5.60 s | 7.83 GB | passed |
| 0.6B LM requested and used | 110.02 s | 269.41 s | 10.75 GB | passed |

All three runs produced a 48 kHz stereo, exactly 30.0-second, finite,
non-silent, non-clipped WAV measured at -14.3 LUFS and -1.0 dBFS true peak. The
fresh and warm DiT runs reproduced the same artifact checksum at the fixed seed.
The LM run proved initialization and execution through non-zero LM timings and
higher VRAM, but its output checksum was also byte-identical to the DiT-only
artifact. This evidence therefore proves that the LM path ran, not that it
changed this fully specified instrumental request.

The sanitized run records are:

- [benchmark-dit-only-fresh.json](benchmark-dit-only-fresh.json)
- [benchmark-dit-only-warm.json](benchmark-dit-only-warm.json)
- [benchmark-lm-0.6b.json](benchmark-lm-0.6b.json)

The generated WAV files remain ignored local evidence and are not redistributed
through Git. The T4 session was explicitly terminated after artifact download;
the CLI reported no active sessions.

## Modal T4 and second LM-sensitive fixture

Modal CLI 1.6.3 is authenticated in WSL2 and provides an on-demand alternative
that does not require repeated Google Drive authorization. A persistent Volume
holds the same four checksummed model assets (7,642,647,432 bytes). Initial
download, verification, and persistence took 53.27 seconds; the Modal function
completed in 60.58 seconds, including a 22.60-second persistent copy. The Volume
is retained for later on-demand use, but every experiment app is stopped with
zero active tasks.

Fixture `vi-020` contains short project-authored Vietnamese lyrics and one
code-switch response. On a Tesla T4, its DiT-only vocal generation failed
deterministically because all 48,000 float16 latents became NaN. An explicit
`ACESTEP_DTYPE=float32` retry produced the identical error because the pinned
ACE-Step revision ignored the override and remained on float16. The harness now
records requested and observed precision and rejects an ignored explicit
override before generation instead of silently accepting fallback.

Two isolated 0.6B LM contract probes both initialized and executed the LM, but
returned empty metadata and zero audio codes. The recorded repeat used 4.43 GB
peak allocated VRAM, loaded in 72.58 seconds, and spent 247.49 seconds in LM
inference. This is classified as a deterministic invalid conditioning contract,
not successful LM-sensitive generation. No WAV was created by that vocal probe;
generated-WAV transfer was measured separately with the successful instrumental
reproduction below.

Upstream issues
[#1055](https://github.com/ace-step/ACE-Step-1.5/issues/1055) and
[#1243](https://github.com/ace-step/ACE-Step-1.5/issues/1243) independently
describe the same pre-Ampere vocal float16 overflow and unwired float32 override.
The sanitized records are
[modal-t4-evidence.json](modal-t4-evidence.json) and
[benchmark-lm-contract-vi020.json](benchmark-lm-contract-vi020.json). Billing at
that checkpoint was $0.2597 metered and $0 billed after credits and allowances.

### Float32 fix allocation attempt

A community fork based directly on the pinned source revision was reviewed and
pinned at commit `44c15413f6dc9b55acfbd214209dbb6c0aac74b9`. Its two added
commits wire `ACESTEP_DTYPE`, default pre-Ampere CUDA to float32, and select an
attention implementation for that float32 path. The benchmark harness now
accepts an explicit source repository/revision and records CPU-offload state, so
the fork cannot be mislabeled as the accepted upstream revision.

Three T4 allocation profiles and, after explicit user approval, three L4
profiles were submitted sequentially. Every call remained pending without a
container, start time, or GPU execution and was explicitly cancelled. The L4
attempt limit of three was not exceeded. Consequently, this is provider-capacity
evidence only: the float32 fix has not executed and no generation or quality
claim can be made. All Modal apps are stopped, Colab has no active sessions, and
the persistent model Volume remains available for a later on-demand retry.

The sanitized allocation record is
[modal-float32-fix-allocation.json](modal-float32-fix-allocation.json). The final
billing snapshot is $0.2707 metered and $0 billed after credits and allowances.

## Modal reproduction of the Colab instrumental PoC

On 2026-11-12, the bounded Modal T4 function reproduced the successful Colab
`vi-014` DiT-only contract using the same ACE-Step source revision, checksummed
model assets, fixture, seed 20261111, 30-second duration, eight diffusion steps,
shift 3.0, batch one, LM-off path, and upstream-default precision. The observed
precision was float16 with no CPU offload. Model load took 8.43 seconds,
inference took 5.06 seconds, peak allocated VRAM was 7.83 GB, and the complete
remote benchmark took 26.51 seconds.

The 5,760,078-byte WAV passed remote-to-local size and SHA-256 verification. Its
download took 15.56 seconds at 370,184.96 bytes/second. Like the Colab baseline,
it is 48 kHz stereo, exactly 30.0 seconds, finite, non-silent, non-clipped,
-14.3 LUFS, and -1.0 dBFS true peak. The Modal artifact checksum
`adcde481123030501c46d7eeb2a7cb120ee07b088e7d55807ce4d47c84a06429` differs
from the Colab checksum
`9667cb6dfb8d10a54cfcfdfa1e78dbf52a8ed73039ed71bac9469483a97e390e`.
Modal used driver 620.70.06 and Python 3.12.10, while Colab used driver
580.82.07 and Python 3.12.3; both used a Tesla T4, CUDA 12.8, and Torch
2.10.0+cu128. The original Colab WAV is no longer available locally, so this
evidence proves the same pinned generation contract and matching technical audio
properties, not byte identity or subjective musical equivalence.

The sanitized records are
[benchmark-modal-colab-reproduction.json](benchmark-modal-colab-reproduction.json)
and
[modal-colab-reproduction-verification.json](modal-colab-reproduction-verification.json).
The Modal app scaled to zero with zero active tasks. The final billing snapshot
was $0.30 metered and $0 billed after credits and allowances.

## Pre-S05 provider strategy and transport proof

The product harness now selects its execution provider through an explicit
strategy registry rather than scheduler conditionals. `GPU_RUNNER=modal` is the
application default; changing the value to `colab`, `fake`, or `local` selects
that strategy at startup. Unknown or unavailable strategies fail explicitly,
and no provider, GPU type, or operation silently falls back.

Modal and Colab use the same packaged allowlisted remote-worker file contract.
Modal job content travels through private temporary files rather than CLI
arguments. Health, echo, and unsupported-kind validation use a scale-to-zero
CPU function; `music.generate` is the only current Modal T4 route, with one
container and a two-second scale-down window. Docker and CI remain explicitly
configured for FakeRunner so their credential-free quickstart remains
reproducible.

A real `harness.healthcheck` crossed the SQLite durable queue, strategy registry,
official Modal CLI, remote function, packaged worker, and fenced job completion
in 11.13 seconds. Remote worker execution took 0.012 seconds. The app stopped
with zero tasks and zero active containers. A separate bounded T4 allocation
probe received no container and was classified `TimeoutError` after 303.24
seconds; graceful cancellation stopped the app without fallback or a billing
increase. This is provider-capacity evidence, not a model result. The earlier
successful `vi-014` reproduction remains the real Modal T4 audio proof.

The built wheel contains both remote transport assets, and an isolated wheel
installation resolved both default paths without a repository checkout. The
sanitized record is
[modal-runner-strategy.json](modal-runner-strategy.json). Metered cost remains
$0.30 and billed cost remains $0 after credits and allowances.

## Backend and license decision

[ADR-0004](../../adr/0004-ace-step-is-research-only-on-t4.md) rejects the pinned
ACE-Step candidate as Melqivra's default production backend. Its verified
instrumental T4 evidence remains useful for research, but vocal, LM, reference,
repaint, extend, and continuation capabilities are unsupported until a
separately pinned backend passes their real-GPU contracts.
[ADR-0005](../../adr/0005-modal-is-the-default-remote-transport.md) separately
makes Modal the default execution transport while keeping Colab explicitly
selectable and forbidding automatic provider fallback. Default transport
selection does not accept a production music backend.

The license review is complete for non-bundled research use. ACE-Step source and
model cards declare MIT; the Qwen3 base lineage declares Apache-2.0. Melqivra
does not bundle or mirror weights, and release notices preserve both attribution
boundaries. The machine-readable record is
[license-review.json](license-review.json). Weight redistribution remains
unapproved and would require a new review.

## Local quality gate

The current checkpoint passed the complete repository gate on Windows/Python
3.11: 112 tests, strict mypy across 36 source files, Ruff format/lint, 83% branch
coverage for the scheduler/store critical-path scope, wheel/sdist build, and
dependency audit with no known vulnerabilities. The unpublished local
`melqivra` package was skipped by the audit as expected.

## Next evidence

1. Retry the exact float32-fix commit only when Modal can allocate a GPU, then
   decide whether it changes the T4 vocal failure; do not treat pending capacity
   as a model result.
2. Exercise reference, repaint, and extend only when upstream reports those
   capabilities explicitly; unsupported operations must fail clearly.
3. Add the remaining controlled OOM classification without treating the vocal
   numerical failure as an OOM.
