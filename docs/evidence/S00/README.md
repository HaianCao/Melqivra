# S00 evidence — repository governance and reproducibility

Status: `IN_PROGRESS`

Recorded: 2026-11-11 (Asia/Saigon)

Final candidate commit tested: `dfee7546864321fd15697d28ebcd8b5b18ede990`

## Environment

- Windows x86_64
- Local Python 3.11.17 and uv 0.12.23
- Hosted Python 3.11 and 3.13 on Ubuntu and Windows
- CPU-only/FakeRunner test mode
- Docker Desktop 4.74.0 with Docker Engine 29.4.3
- Linux/amd64 container with Python 3.12.16

## Commands executed

```shell
uv sync --locked --extra dev
uv run --frozen ruff format --check src tests scripts
uv run --frozen ruff check src tests scripts
uv run --frozen mypy
uv run --frozen coverage run -m unittest discover -s tests -v
uv run --frozen coverage report
uv build
uv run --frozen pip-audit
docker compose -p melqivra-s00 build --no-cache
docker compose -p melqivra-s00 up -d --no-build
docker compose -p melqivra-s00 exec -T harness melqivra doctor
# Submit and inspect one harness.echo job through the documented HTTP API.
docker compose -p melqivra-s00 restart harness
docker compose -p melqivra-s00 down --volumes
```

## Local and container results

- Locked environment creation: pass.
- Ruff format and lint: pass.
- Strict mypy: 14 source files, no issues.
- Unit/integration tests: 21 pass.
- Critical scheduler/store branch coverage: 81%, threshold 75%.
- Source distribution and wheel build: pass.
- Dependency audit: no known vulnerability; the unpublished local `melqivra`
  package was correctly skipped as not present on PyPI.
- Importing `agent_music` and `agent_music.api` in an empty working directory
  creates no runtime state.
- FakeRunner doctor: pass.
- Missing Colab CLI diagnostic: explicit non-zero result, no crash.
- A no-cache Linux/amd64 image build used `uv.lock`, uv 0.13.6, and the
  `python:3.12-slim` base image at
  `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`.
- Current Melqivra image ID:
  `sha256:ff382cc4553b8b1b6ee5097d2fa727ae8a67992e73b2e3033d36b7c405ddc2f5`.
- Container startup, the `melqivra doctor` CLI, HTTP enqueue, and successful
  `harness.echo` completion: pass.
- The completed job remained queryable after a container restart using the
  Compose volume.
- Runtime process executed as the non-root `appuser` with UID/GID 10001.
- Disposable container, network, and evidence volume were removed afterward.

## Hosted CI

GitHub Actions run
[`42968008929`](https://github.com/HaianCao/Melqivra/actions/runs/42968008929)
tested `dfee7546864321fd15697d28ebcd8b5b18ede990` and completed successfully:

- [Python 3.11 on Ubuntu](https://github.com/HaianCao/Melqivra/actions/runs/42968008929/job/130349637477)
- [Python 3.13 on Ubuntu](https://github.com/HaianCao/Melqivra/actions/runs/42968008929/job/130349637102)
- [Python 3.11 on Windows](https://github.com/HaianCao/Melqivra/actions/runs/42968008929/job/130349637353)
- [Python 3.13 on Windows](https://github.com/HaianCao/Melqivra/actions/runs/42968008929/job/130349637374)

Every matrix job passed locked installation, format, lint, strict typing,
critical-path coverage tests, and the coverage threshold. The designated Ubuntu
3.13 job also built the package and completed the dependency audit.

## Repository controls

The following GitHub controls were verified through the repository API on
2026-11-11 and are captured immutably by this evidence revision:

- The public repository defaults to `main`; Issues and Discussions are enabled.
- `main` requires the four hosted matrix checks above with strict up-to-date
  status checking.
- Pull requests are required for administrators and contributors; stale reviews
  are dismissed, linear history and conversation resolution are required, and
  force pushes and branch deletion are disabled.
- Private vulnerability reporting is enabled.
- Dependabot vulnerability alerts and automated security updates are enabled.
- Secret scanning and secret-scanning push protection are enabled.

Machine-readable results are in [quality-results.json](quality-results.json).

## Remaining closure gates

S00 remains deliberately `IN_PROGRESS` until this evidence update is merged
through the protected branch, the complete gate is rerun on the resulting final
candidate, and the dedicated closure commit and annotated
`stage-s00-repository` tag are created.
