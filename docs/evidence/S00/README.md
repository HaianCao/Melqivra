# S00 evidence — repository governance and reproducibility

Status: `IN_PROGRESS`

Recorded: 2026-11-11 (Asia/Saigon)

Source commit tested: `43bcfd0824e84e79241276fef5cf6168d689149f`

## Environment

- Windows x86_64
- Python 3.13.9
- uv 0.12.23
- CPU-only/FakeRunner test mode
- No Docker daemon available during this evidence run

## Commands executed

```shell
uv sync --locked --extra dev --cache-dir .uv-cache
uv run --frozen pre-commit run --all-files
uv run --frozen mypy
uv run --frozen coverage run -m unittest discover -s tests -q
uv run --frozen coverage report -m
uv build
uv run --frozen pip-audit
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

Machine-readable results are in [quality-results.json](quality-results.json).

## Remaining closure gates

S00 is deliberately not marked `DONE` yet:

1. The committed Windows/Linux Python 3.11/3.13 GitHub Actions matrix has not
   been observed on a hosted runner.
2. Docker Desktop was not running, so a Linux container build/quickstart could
   not be used as substitute clean-environment evidence.
3. Repository-host branch protection and private vulnerability reporting are
   external GitHub settings and cannot be verified in this local-only checkout.

When at least one clean Linux path and repository settings are verified, append
their immutable run references here, run the full gate again, then create the
S00 closure commit and `stage-s00-repository` annotated tag.

