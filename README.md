# Melqivra

[![CI](https://github.com/HaianCao/Melqivra/actions/workflows/ci.yml/badge.svg)](https://github.com/HaianCao/Melqivra/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Melqivra is an open-source, chat-controlled music studio for developing a song
from an idea or partial lyrics into structured revisions, approved generation
plans, multiple audio takes, focused edits, and provenance-rich exports.

The project is being built in evidence-gated stages. The current implementation
includes immutable song-project revisions, a safe provider-neutral agent tool
boundary, and a CPU-only creative workflow that turns an idea or authorized
lyrics into a reviewable brief, blueprint, lyrics revision, and capability-aware
generation plan. Real product audio generation is not yet implemented.

Repository: <https://github.com/HaianCao/Melqivra>

Project policies: [contributing](CONTRIBUTING.md),
[security](SECURITY.md), [support](SUPPORT.md),
[code of conduct](CODE_OF_CONDUCT.md), and
[architecture decisions](docs/adr/README.md).

Development references: [support matrix](docs/development/support-matrix.md),
[dependency policy](docs/development/dependency-policy.md), and
[release policy](docs/development/release-policy.md). Model and weight candidates
are tracked separately in [MODEL_LICENSES.md](MODEL_LICENSES.md); the repository
license does not grant redistribution rights for those assets.

## Available today

- Durable SQLite FIFO queue.
- Per-user idempotency keys.
- Fencing tokens that reject stale worker commits.
- Exactly one concurrent GPU job.
- On-demand runner provisioning.
- Five-minute idle grace period, configured by
  GPU_IDLE_TIMEOUT_SECONDS with a default of 300.
- Session reuse when another job arrives during the grace period.
- Strategy-selected Modal or Colab runner using argument arrays without shell
  interpolation.
- Fake runner for development and CI without a GPU.
- FastAPI endpoints for submit, inspect, cancel, and runner status.
- One self-contained allowlisted health/transport worker shared by Modal and
  Colab.
- Immutable, checksummed project revisions and portable project archives.
- OpenAI-compatible planner client with allowlisted authenticated domain tools.
- Structured briefs and duration-balanced blueprints.
- Lock-safe lyrics generation, continuation, restructuring, critique, and
  constrained rewriting.
- Vietnamese syllable, tone, rhyme, Unicode, and mixed-language warnings.
- Identity/copyright safeguards and explicit lyrics-rights declarations.
- Capability-aware 2–4 take planning and human-readable contract review.

S05 has now produced two validated 20-second HeartMuLa vocal takes through the
durable Modal T4 path and persisted them with immutable manifests. HeartMuLa is
a scoped candidate for its declared `zh`, `en`, `ja`, `ko`, and `es` vocal
capabilities; it is not a universal default, does not claim Vietnamese or
instrumental support, and has a narrow measured T4 memory margin. The pinned
ACE-Step path remains research-only for its verified 30-second instrumental
contract. See [the S05 evidence](docs/evidence/S05/README.md).

Run the S04 CPU-only creative-contract demonstration:

~~~shell
uv run --frozen python scripts/s04_creative_demo.py
~~~

It uses synthetic Vietnamese fixtures, makes no provider request, and starts no
GPU runner. See [the S04 evidence](docs/evidence/S04/README.md).

## Product direction

Melqivra is designed around a few durable rules:

- Chat is the primary control surface, but typed domain tools are the authority.
- Briefs, lyrics, blueprints, settings, and takes are immutable revisions.
- Expensive GPU work requires an explicit durable job and approval boundary.
- Unsupported model capabilities fail explicitly instead of silently falling
  back to a different operation or model.
- Artifacts carry revision, model, configuration, seed, license, lineage, and
  checksum provenance.
- Voice identity features remain disabled until consent, revocation, deletion,
  leakage evaluation, and abuse controls are implemented.

## Requirements

- Python 3.11 or newer.
- uv is recommended.
- For default Modal mode: the official Modal CLI, an authenticated account, and
  a valid workspace. Run the API in the same Linux/WSL environment that owns the
  Modal profile.
- For Colab mode: Google Colab CLI running on Linux or macOS. On Windows, use
  WSL2 or a Linux container because upstream does not currently support native
  Windows.
- A Colab plan/account entitled to provision T4 runtimes.

## Local development

~~~shell
uv sync --extra dev
uv run melqivra doctor
uv run melqivra serve --host 127.0.0.1 --port 8000
~~~

The earlier `agent-music` command remains available as a compatibility alias
during the pre-release period.

Modal T4 is the application default. Set `GPU_RUNNER=fake` for credential-free
CPU-only development and CI. Provider selection is explicit and never falls
back automatically:

| `GPU_RUNNER` | Strategy | Lifecycle |
|---|---|---|
| `modal` | Modal | CPU transport checks; T4 only for GPU jobs; scales to zero |
| `colab` | Named Colab T4 session | Starts on demand; scheduler stops after idle grace |
| `fake` | Deterministic CPU fake | Development and CI only |
| `local` | Local CUDA placeholder | Fails until the S11 implementation exists |

SQLite remains the safe development job store. The in-progress S07 PostgreSQL
adapter is opt-in and never falls back to SQLite:

~~~shell
uv sync --locked --extra postgres
~~~

~~~dotenv
JOB_STORE=postgres
POSTGRES_DSN=postgresql://user:password@database:5432/melqivra
GPU_JOB_MAX_ATTEMPTS=3
~~~

`melqivra doctor` checks the PostgreSQL connection and applies only tracked,
checksummed migrations. Each accepted job snapshots the configured positive
attempt limit; an expired final attempt becomes `dead_lettered` instead of being
retried forever. The same migration set now includes tenant-owned projects,
immutable checksummed revisions, and optimistic working/accepted pointers behind
the provider-neutral `ProjectRepository` port. It also includes exact one-way
approvals plus restart-safe tool-call replay and append-only, content-minimal
audit records. Durable job events use per-job sequences and can be replayed after
an API restart.

Production artifact publication is available through the optional locked
object-storage dependency set:

~~~shell
uv sync --locked --extra postgres --extra object-storage
~~~

~~~dotenv
JOB_STORE=postgres
ARTIFACT_STORAGE=s3
S3_ENDPOINT_URL=https://s3-compatible.example
S3_BUCKET=melqivra-artifacts
S3_REGION=us-east-1
S3_ADDRESSING_STYLE=auto
~~~

PostgreSQL reserves tenant/project ownership, lineage, checksum, and publication
state before any upload. S3/MinIO stores immutable bytes only. A batch remains
invisible until every object has been downloaded back and checksum-verified;
exact retries are idempotent and changed retries conflict. `melqivra doctor`
checks both authorities without printing the DSN, endpoint credentials, or
object keys. Credentials use the standard AWS provider chain and must not be
placed in committed files.

Artifact cleanup is an explicit operational pass, not an import-time side
effect. It recovers complete interrupted publications, removes incomplete stale
batches, resumes crash-interrupted deletions, applies optional published-byte
retention, and deletes only untracked objects older than the configured orphan
grace period:

~~~dotenv
ARTIFACT_PENDING_TIMEOUT_SECONDS=3600
ARTIFACT_CORRUPT_RETENTION_SECONDS=86400
ARTIFACT_ORPHAN_GRACE_SECONDS=86400
# Empty disables automatic retention of healthy published artifacts.
ARTIFACT_PUBLISHED_RETENTION_SECONDS=
ARTIFACT_RECONCILE_BATCH_LIMIT=100
ARTIFACT_RECONCILE_OBJECT_PAGE_SIZE=100
~~~

~~~shell
uv run melqivra reconcile-artifacts
~~~

The command prints content-free counters. If `next_orphan_cursor` is non-null,
run another bounded page with `--orphan-cursor '<cursor>'`. Deletion first commits
a PostgreSQL `deleting` tombstone, removes every immutable object idempotently,
and only then commits `deleted`; a crash at any point is safe to retry.
Backup/restore and the full multi-process restart demo remain in-progress S07
work.

Submit a job:

~~~shell
curl -X POST http://127.0.0.1:8000/v1/gpu/jobs \
  -H "Content-Type: application/json" \
  -H "X-User-Id: demo-user" \
  -d '{"kind":"harness.echo","payload":{"message":"hello"}}'
~~~

Inspect runner state:

~~~shell
curl http://127.0.0.1:8000/v1/gpu/runner \
  -H "X-User-Id: demo-user"
~~~

## Modal T4 mode (default)

Install and authenticate the official CLI in Linux/WSL:

~~~shell
uv tool install modal==1.6.3
modal setup
modal token info
~~~

Copy `.env.example` to `.env`. The default selection is:

~~~dotenv
GPU_RUNNER=modal
MODAL_CLI_VERSION=1.6.3
MODAL_APP_NAME=melqivra-gpu-runner
TRANSFER_ROOT=data/transfers
~~~

Run `uv run melqivra doctor` from the same environment that owns the Modal
profile. The scheduler submits only file-backed requests. Health, echo, and
unsupported-kind validation run without a GPU; `music.generate` is fixed to a
T4 with one container. Every function scales to zero after each job. Use
`harness.healthcheck` for the real transport smoke test. Private payload content
is not placed in CLI arguments or provider log events. The Modal app and shared
worker are installed package assets, so the default also works from a built
wheel rather than requiring a repository checkout.

A real `music.generate` backend that stores output in a Modal Volume must also
set `MODAL_ARTIFACT_VOLUME` and its safe relative `MODAL_ARTIFACT_PREFIX`.
Declared output files are downloaded into a job-scoped transfer directory,
independently revalidated, and atomically imported before the durable job can be
marked successful. Backend routing is capability-based: the agent may recommend
compatible exact revisions, but the client approves the chosen backend and
contract, and no provider/model/precision/operation fallback occurs. The default
general-purpose app still fails explicitly until an accepted plugin and its
model volume are configured.

Switch to Colab by changing one setting:

~~~dotenv
GPU_RUNNER=colab
~~~

## Colab T4 mode

Install and authenticate the official CLI in Linux/WSL:

~~~shell
uv tool install google-colab-cli==0.8.0
colab version
colab new -s smoke --gpu T4
colab stop -s smoke
~~~

Copy .env.example to .env, then set:

~~~dotenv
GPU_RUNNER=colab
GPU_MAX_CONCURRENCY=1
GPU_IDLE_TIMEOUT_SECONDS=300
COLAB_CLI_VERSION=0.8.0
COLAB_SESSION_NAME=melqivra-t4
COLAB_GPU=T4
COLAB_AUTH_PROVIDER=oauth2
~~~

Run the API from the same Linux/WSL environment that owns the Colab
credentials. A real transport smoke job uses kind harness.healthcheck.

The scheduler performs:

1. colab status for the named session.
2. colab new --gpu T4 only if no session exists.
3. Optional Drive mount.
4. Upload the job request.
5. Execute the packaged `agent_music/remote/worker.py` allowlisted worker.
6. Download and validate the JSON result.
7. Keep the session warm while the queue is active.
8. Call colab stop after 300 seconds with no queued or running job.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | /v1/gpu/jobs | Enqueue one job |
| GET | /v1/gpu/jobs | List own jobs with cursor pagination |
| GET | /v1/gpu/jobs/{id} | Inspect own job |
| GET | /v1/gpu/jobs/{id}/events | Stream durable progress with SSE replay |
| DELETE | /v1/gpu/jobs/{id} | Request cancellation |
| GET | /v1/artifacts/{id} | Download one authorized, checksum-verified artifact |
| GET | /v1/gpu/runner | Runner/session/idle deadline |
| GET | /healthz | Process health |

Every GPU endpoint requires an authenticated principal. `API_AUTH_MODE` defaults
to `development`, where `X-User-Id` is an explicit local-only identity adapter.
Production deployments set `API_AUTH_MODE=injected` and pass an `Authenticator`
to the application factory; the included static bearer adapter is suitable for
bounded deployments and tests without coupling domain logic to an identity SDK.
The service returns stable error codes, propagates or creates `X-Request-ID`,
limits payload size and active jobs per tenant, and never exposes lease owners or
fencing tokens. Cursor values are tenant-scoped and cannot be reused to enumerate
another user's jobs.

Job streams use standard server-sent events. Each durable event has a monotonic
per-job `id`; reconnect with `Last-Event-ID` to receive only later events. A
missing or cross-tenant job remains indistinguishable, and a cursor beyond the
authoritative stream is rejected instead of silently skipping data:

~~~shell
curl -N http://127.0.0.1:8000/v1/gpu/jobs/JOB_ID/events \
  -H "X-User-Id: demo-user" \
  -H "Last-Event-ID: 12"
~~~

## Docker

~~~shell
docker compose up --build
~~~

The default container deliberately uses the fake runner so the credential-free
quickstart remains reproducible. Set `COMPOSE_GPU_RUNNER` only in a container
image that also contains the selected provider CLI and isolated credentials.

## Verification

The standard-library test suite does not require installed project
dependencies:

~~~shell
python -m unittest discover -s tests -v
~~~

The complete local quality gate is:

~~~shell
uv sync --locked --extra dev
uv run --frozen ruff format --check src tests scripts
uv run --frozen ruff check src tests scripts
uv run --frozen mypy
uv run --frozen coverage run -m unittest discover -s tests -v
uv run --frozen coverage report
uv build
uv run --frozen pip-audit
~~~

Install the repository-managed hooks with `uv run pre-commit install
--install-hooks --hook-type pre-commit --hook-type pre-push`. CI runs the same
core gates on Python 3.11 and 3.13 on both Linux and Windows. GitHub Actions are
pinned to immutable commit SHAs.

Tests cover the durable GPU harness plus immutable project history, safe agent
tools, approvals, creative planning, lock invariance, contract pinning, and
Vietnamese lyrics validation.

## License

Source code is licensed under Apache-2.0. Model weights retain their own licenses
and are not bundled in this repository.

