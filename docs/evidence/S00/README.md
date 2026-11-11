# S00 evidence — repository governance and reproducibility

Status: `IN_PROGRESS`

Recorded: 2026-11-11 (Asia/Saigon)

Source commit tested: `47bab7833485a9a0211155aad495ae96b2192fed`

## Environment

- Windows x86_64
- Python 3.13.9
- uv 0.12.23
- CPU-only/FakeRunner test mode
- Docker Desktop 4.74.0 with Docker Engine 29.4.3
- Linux/amd64 container with Python 3.12.16

## Commands executed

```shell
uv sync --locked --extra dev --cache-dir .uv-cache
uv run --frozen pre-commit run --all-files
uv run --frozen mypy
uv run --frozen coverage run -m unittest discover -s tests -q
uv run --frozen coverage report -m
uv build
uv run --frozen pip-audit
docker compose -p agent-music-s00 build --no-cache
docker compose -p agent-music-s00 up -d --no-build
docker compose -p agent-music-s00 exec -T harness agent-music doctor
# Submit and inspect one harness.echo job through the documented HTTP API.
docker compose -p agent-music-s00 restart harness
docker compose -p agent-music-s00 down --volumes
```

## Results

- Locked environment creation: pass.
- Ruff lint and format: pass.
- Strict mypy: 14 source files, no issues.
- Unit/integration tests: 20 pass.
- Critical scheduler/store branch coverage: 80%, threshold 75%.
- Source distribution and wheel build: pass.
- Dependency audit: no known vulnerability; the local unpublished
  `agent-music` package was correctly skipped as not present on PyPI.
- Import safety: importing `agent_music` and `agent_music.api` in an empty working
  directory creates no runtime state.
- FakeRunner doctor: pass.
- Missing Colab CLI diagnostic: explicit non-zero result, no crash.
- A no-cache Linux/amd64 image build used `uv.lock` with uv 0.13.6 and the
  `python:3.12-slim` base image at
  `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`.
- Built image ID:
  `sha256:ba6662ab76bd015715dbc4a2563be192b235ff6826938138999ff1248c83c6ff`.
- Container health, FakeRunner doctor, HTTP enqueue, and successful
  `harness.echo` completion: pass.
- The completed job remained queryable after a container restart using the
  Compose volume.
- Runtime process executed as the non-root `appuser` with UID/GID 10001.
- Disposable container, network, and evidence volume were removed afterward.

Machine-readable results are in [quality-results.json](quality-results.json).

## Remaining closure gates

S00 is deliberately not marked `DONE` yet:

1. The committed Windows/Linux Python 3.11/3.13 GitHub Actions matrix has not
   been observed on a hosted runner.
2. Repository-host branch protection and private vulnerability reporting are
   external GitHub settings and cannot be verified in this local-only checkout.

The clean Linux container path is now verified. When the hosted matrix and
repository settings are verified, append their immutable references here, run
the full gate again, then create the S00 closure commit and
`stage-s00-repository` annotated tag.

