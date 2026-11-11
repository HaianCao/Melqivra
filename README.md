# Agent Music

Agent Music is an open-source, chat-controlled music studio. This repository
currently contains the first production component: a durable single-GPU
harness that starts a Google Colab T4 only when work arrives, serializes jobs
from multiple users, keeps the session warm for five minutes, and then shuts it
down automatically when the queue remains empty.

Project policies: [contributing](CONTRIBUTING.md),
[security](SECURITY.md), [support](SUPPORT.md),
[code of conduct](CODE_OF_CONDUCT.md), and
[architecture decisions](docs/adr/README.md).

## Implemented in this stage

- Durable SQLite FIFO queue.
- Per-user idempotency keys.
- Fencing tokens that reject stale worker commits.
- Exactly one concurrent GPU job.
- On-demand runner provisioning.
- Five-minute idle grace period, configured by
  GPU_IDLE_TIMEOUT_SECONDS with a default of 300.
- Session reuse when another job arrives during the grace period.
- Colab CLI runner using argument arrays without shell interpolation.
- Fake runner for development and CI without a GPU.
- FastAPI endpoints for submit, inspect, cancel, and runner status.
- Self-contained Colab-side health/transport worker.

The ACE-Step generation plugin is intentionally the next stage. A
music.generate job currently fails clearly until agent_music_worker has
been installed in the Colab runtime; harness health jobs already exercise the
real Colab transport.

## Requirements

- Python 3.11 or newer.
- uv is recommended.
- For Colab mode: Google Colab CLI running on Linux or macOS. On Windows, use
  WSL2 or a Linux container because upstream does not currently support native
  Windows.
- A Colab plan/account entitled to provision T4 runtimes.

## Local development

~~~shell
uv sync --extra dev
uv run agent-music doctor
uv run agent-music serve --host 127.0.0.1 --port 8000
~~~

Fake mode is the default and requires no GPU.

Submit a job:

~~~shell
curl -X POST http://127.0.0.1:8000/v1/gpu/jobs \
  -H "Content-Type: application/json" \
  -H "X-User-Id: demo-user" \
  -d '{"kind":"harness.echo","payload":{"message":"hello"}}'
~~~

Inspect runner state:

~~~shell
curl http://127.0.0.1:8000/v1/gpu/runner
~~~

## Colab T4 mode

Install and authenticate the official CLI in Linux/WSL:

~~~shell
uv tool install google-colab-cli
colab version
colab new -s smoke --gpu T4
colab stop -s smoke
~~~

Copy .env.example to .env, then set:

~~~dotenv
GPU_RUNNER=colab
GPU_MAX_CONCURRENCY=1
GPU_IDLE_TIMEOUT_SECONDS=300
COLAB_SESSION_NAME=agent-music-t4
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
5. Execute scripts/colab/remote_worker.py.
6. Download and validate the JSON result.
7. Keep the session warm while the queue is active.
8. Call colab stop after 300 seconds with no queued or running job.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | /v1/gpu/jobs | Enqueue one job |
| GET | /v1/gpu/jobs/{id} | Inspect own job |
| DELETE | /v1/gpu/jobs/{id} | Request cancellation |
| GET | /v1/gpu/runner | Runner/session/idle deadline |
| GET | /healthz | Process health |

Every job endpoint requires X-User-Id. This is a development identity
boundary; the application authentication system will replace it in a later
stage.

## Docker

~~~shell
docker compose up --build
~~~

The default container uses the fake runner. Colab OAuth and CLI support should
be configured in WSL2 first; a dedicated credential-isolated Colab broker
container is planned after the ACE-Step worker stage.

## Verification

The standard-library test suite does not require installed project
dependencies:

~~~shell
python -m unittest discover -s tests -v
~~~

Tests cover on-demand start, strict serialization across users, warm-session
reuse, idle shutdown, FIFO ordering, idempotency, and fencing.

## License

Source code is licensed under Apache-2.0. Model weights retain their own licenses
and are not bundled in this repository.

