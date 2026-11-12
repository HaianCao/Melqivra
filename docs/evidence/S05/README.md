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

Generation submission now creates a pending GPU approval bound to the accepted
contract SHA-256. Pending, rejected, cross-user, cross-project, stale-checksum,
or changed-revision requests cannot enqueue work. Only the exact approved
contract serializes into a durable `music.generate` job.

The isolated HeartMuLa candidate adapter uses the pinned upstream revision and
model IDs, passes approved tags and lyrics unchanged, requires every sampling
parameter explicitly, fixes music/codec precision to FP16/FP32, enables upstream
lazy loading, pins seeds per take, normalizes output to PCM16 WAV, and rejects
silent, clipped, truncated, incomplete, or path-escaping results. The installed
`agent_music_worker` now has a real typed execution boundary, but it remains
default-off until `MUSIC_BACKEND` and the verified model root are configured.
The complete pin includes the metadata/tokenizer repository required by upstream,
and the adapter explicitly neutralizes upstream's implicit lowercase transform so
approved casing is not silently changed before tokenization.

The exact cost-bounded experiment is recorded in
[heartmula-probe-contract.json](heartmula-probe-contract.json). It permits one
durable two-take, 20-second-per-take English vocal job only after the local gate,
CPU-only Modal image contract, and checksummed cache population succeed. It has
one T4 container, a two-second scale-down window, no retry, and no provider,
precision, model, or GPU fallback.

The CPU/image/cache result is recorded in
[heartmula-runtime-preflight.json](heartmula-runtime-preflight.json). After two
cheap CPU-only bootstrap failures were corrected, the Python 3.11 image passed
the exact source hash/import contract. The 22,400,063,678-byte cache then
downloaded and verified all six large weight SHA-256 values in 121.64 seconds.
No GPU was allocated; all apps are stopped, active containers are zero, and
billed cost remains $0 before the authorized T4 job.

The authorized durable Modal job then succeeded with two distinct 20.08-second,
48 kHz stereo PCM16 vocal takes. Remote validation rejected neither silence nor
clipping; the two seeds and SHA-256 values differ as required. The worker spent
244.23 seconds and the durable end-to-end job spent 480.06 seconds. Peak VRAM
was 15,272,909,312 bytes for the first take and 14,803,810,816 bytes for the
second, leaving only 0.78 GiB and 1.21 GiB on a 15 GiB T4. The technical
contract passed, but the safety margin is narrow and subjective music quality,
instrumental behavior, and repeated-seed determinism are not yet claimed.
Both WAVs downloaded in 6.51 seconds and passed independent local size,
SHA-256, PCM16, duration, finite-sample, silence, and clipping verification in
[heartmula-t4-verification.json](heartmula-t4-verification.json).

The complete local gate passes with 147 tests, strict mypy across 48 source
files, Ruff format/lint, 83% scheduler/store critical-path branch coverage,
successful wheel/sdist builds, and no known audited vulnerabilities.

## Remaining before closure

1. Have the user review the two vocal takes for subjective musical/vocal quality.
2. Decide whether the narrow T4 VRAM margin is acceptable or requires a safer
   backend/runtime before production acceptance.
3. Design the separate instrumental-capability experiment, but do not allocate
   another GPU without explicit cost approval.
4. Persist validated takes as immutable project artifacts/manifests rather than
   evidence-only Modal Volume files.
