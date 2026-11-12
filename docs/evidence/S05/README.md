# S05 evidence — First audio vertical slice

Status: `IN_PROGRESS`

Recorded from: 2026-11-12 (Asia/Saigon)

## User-approved scope

- The agent derives language, vocal or instrumental intent, style, structure,
  lyrics, and duration through the S04 conversation and planning tools.
- GPU work must not start until the client approves the complete immutable
  generation contract.
- One approved request produces two to four explicit variations with distinct
  seeds and immutable artifact/manifests.
- Unsupported languages, vocal modes, durations, models, revisions, or output
  contracts fail before or at the authoritative job boundary without fallback.
- Voice identity, direct recording, uploaded reference media, song editing, and
  waveform-preserving repaint remain outside the initial S05 backend claim.

## Backend preflight

The CPU-only candidate audit is recorded in
[backend-candidates.json](backend-candidates.json). HeartMuLa is the only current
probe candidate that combines a permissive license with declared multilingual
lyrics-to-song behavior. It is not accepted yet: T4 FP16 execution, memory,
instrumental behavior, determinism, validation, and artifact transfer remain to
be proved by one bounded evaluation after the adapter and tests are complete.

SongGeneration is rejected for production because its license restricts use to
academic, research, and education purposes. YuE2 is rejected for the reference
deployment because its supported baseline requires a BF16-capable GPU with 24 GB
VRAM rather than a Tesla T4.

## Current implementation checkpoint

The initial `MusicGenerationBackend` contract pins backend/model/license
identity, exact source revisions, language, vocal intent, approved contract
checksum, two-to-four take seeds, output integrity, and all-or-nothing batch
validation. No model assets are bundled or downloaded by this checkpoint.

The complete local gate passes with 128 tests, strict mypy across 39 source
files, Ruff format/lint, 83% scheduler/store critical-path branch coverage,
successful wheel/sdist builds, and no known audited vulnerabilities.

## Remaining before closure

1. Connect an explicit client approval to durable `music.generate` submission.
2. Implement and CPU-test the isolated HeartMuLa candidate adapter and worker.
3. Run one bounded T4 preflight, then continue only if the backend passes memory,
   audio, language, vocal/instrumental, determinism, and licensing gates.
4. Persist validated take artifacts/manifests and prove 2+ playable takes E2E.
