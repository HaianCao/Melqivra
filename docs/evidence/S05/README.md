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

The candidate audit is recorded in [backend-candidates.json](backend-candidates.json).
HeartMuLa is now a verified S05 vocal candidate for its five declared languages;
instrumental behavior, repeated-seed determinism, Vietnamese, and languages
outside those declarations remain unsupported. SongGeneration remains rejected
for its restrictive license, and YuE2 remains outside the T4 reference target.

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
The client reviewed the result as good and preferred Take B. That feedback is
preserved separately in [heartmula-user-review.json](heartmula-user-review.json)
so subjective preference does not rewrite the original technical run record.
HeartMuLa remains one option rather than an exclusive default.

The CPU-side persistence boundary now decodes untrusted remote batch results,
revalidates the exact backend/request/take identities, independently checks each
transferred WAV, and atomically publishes all audio plus per-take reproducibility
manifests as one immutable project-scoped batch. Exact retries are idempotent;
tampered retries, path escape, integrity changes, invalid audio, and interrupted
publication fail without exposing a partial take set. Manifests pin source
revisions, approved contract checksum, backend/model/license revisions, seed,
generation settings, environment, audio metadata, and artifact checksum without
duplicating private prompt or lyric text.

Successful durable completion is now wired through that importer. Modal downloads
only the artifact paths declared by the remote result from an explicitly
configured Volume into a job-scoped transfer directory. The scheduler processes
the transfer before its fenced SQLite success commit; import failure records a
failed job with no false result. The sanitized take/artifact/manifest references
survive database restart, while private creative text and machine-local transfer
paths are excluded from the stored result.

The two existing real T4 WAVs were then exercised through that completion path
without allocating another GPU. Both audio files and both manifests were
atomically published, an exact idempotent retry returned the same job, the
sanitized result survived a SQLite restart, all persisted artifact checksums
matched, and Take B resolved to its stable persisted take ID. The result is in
[heartmula-durable-import.json](heartmula-durable-import.json); the private
artifact tree remains ignored under `data/`.

ADR-0006 now defines explicit multi-backend routing. The agent may analyze
language, vocal/instrumental intent, operation, and duration and recommend every
compatible exact backend pin, but the client must approve the selected backend
before GPU work. There is no automatic provider, backend, precision, language,
or operation fallback. Current product routing offers HeartMuLa vocals only for
its declared languages and the clean pinned ACE-Step contract only for the
verified 30-second instrumental research path.

The ignored Colab notebook was reconstructed into the privacy-safe
[ace-step-colab-poc-contract.json](ace-step-colab-poc-contract.json). It confirms
that the earlier Vietnamese result used a 140-second reference-conditioned
turbo path, seed 12345, eight steps, shift 3.0, thinking enabled, LM
initialization called but observed as unavailable and unused, and float32
latents after an in-place pre-Ampere patch. Because the clone was unpinned and
the reference audio is private, this is PoC evidence, not a reproducible backend
claim.

The smallest next ACE diagnostic is prepared but not authorized in
[ace-step-vietnamese-reference-probe-contract.json](ace-step-vietnamese-reference-probe-contract.json).
It permits one 20-second T4 take, no retry or fallback, and a hard bounded
timeout on the pinned community float32 fork. Execution remains blocked until
the client records reference-media ownership/consent and approves a concrete
provider cost ceiling.

The requested local video-to-audio preparation is complete without provider or
GPU use. A 20-second mono 48 kHz PCM16 clip was extracted from the singing-only
video, is 1,920,078 bytes, and passed finite/non-silent/non-clipped validation.
Only checksums and technical properties are tracked in
[ace-step-reference-preflight.json](ace-step-reference-preflight.json); the
video, WAV, and private caption/lyrics remain ignored. The executable Modal
entrypoint refuses to call the remote T4 function unless consent, provider-price
precheck, a positive cost ceiling, the exact source revision, and both private
file hashes are present in a separate authorization record.

The current complete local gate passes with 169 tests, strict mypy across 57
source files, Ruff format/lint, 84% scheduler/store critical-path branch
coverage, successful wheel/sdist builds, and no known audited vulnerabilities.
The WSL Modal entrypoint help contract loaded without creating an app or
container. No GPU was allocated; all Modal apps and active containers are zero,
and billed cost remains $0.

## Remaining before closure

1. Keep the narrow HeartMuLa T4 memory warning visible; do not treat the backend
   as universal or route unsupported languages/instrumental requests to it.
2. Obtain explicit reference-media consent and an exact cost ceiling before the
   prepared ACE Vietnamese reference-vocal experiment. Do not allocate a GPU
   while either field remains unapproved.
3. Run the complete repository gate and protected CI for the routing/evidence
   checkpoint.
4. Close S05 only after the bounded experiment is either completed with honest
   evidence or explicitly deferred without weakening the existing HeartMuLa
   vertical-slice claim.
