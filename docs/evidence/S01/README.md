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

## Second LM-sensitive fixture prepared

Fixture `vi-020` now includes short project-authored Vietnamese lyrics with a
single code-switch response, so the next fixed-seed comparison is vocal rather
than the fully specified instrumental baseline. The benchmark resolves
fixture-defined lyrics explicitly and records privacy-safe SHA-256 fingerprints
for the source caption/lyrics, LM metadata, and LM-generated audio codes. Raw LM
reasoning, generated codes, and WAV artifacts remain outside tracked evidence.

This is tooling readiness only. No second LM-sensitive GPU result is claimed
until paired DiT-only and 0.6B-LM runs complete on the same measured T4 setup.

## Local quality gate

The current checkpoint passed the complete repository gate on Windows/Python
3.11: 48 tests, strict mypy across 16 source files, Ruff format/lint, 81% branch
coverage for the scheduler/store critical-path scope, wheel/sdist build, and
dependency audit with no known vulnerabilities. The unpublished local
`melqivra` package was skipped by the audit as expected.

## Next evidence

1. Run paired DiT-only and 0.6B-LM `vi-020` jobs and compare both conditioning
   fingerprints and artifact checksums without inferring musical quality.
2. Exercise reference, repaint, and extend only when upstream reports those
   capabilities explicitly; unsupported operations must fail clearly.
3. Run OOM/failure classification and an additional fixed-seed comparison.
4. Measure generated-artifact upload/download separately from the now-measured
   model-cache transfer path.
5. Complete license review and accept the backend ADR before closing S01.
