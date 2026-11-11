# Contributing to Agent Music

Thank you for helping build Agent Music. Contributions are accepted when they
improve the product without weakening reproducibility, user control, privacy,
licensing, or the typed tool boundary.

## Before starting

1. Read [ROADMAP.md](ROADMAP.md) and the relevant ADRs under `docs/adr/`.
2. Search existing issues and discussions before proposing a large change.
3. Open an issue before changing a public contract, persistence schema, runner
   lifecycle, model backend, license, or safety policy.
4. Never commit credentials, private lyrics/audio, model weights, runtime
   databases, or generated user artifacts.

## Development setup

Agent Music supports Python 3.11–3.13. The CPU-only fake runner is the default
development mode and requires no GPU.

```shell
uv sync --extra dev
uv run agent-music doctor
uv run python -m unittest discover -s tests -v
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

Use WSL2 or Linux for Colab CLI work. Real GPU tests are optional locally but
must provide the evidence required by their roadmap stage.

## Engineering rules

- Keep domain logic independent of FastAPI, Colab, and concrete LLM SDKs.
- Depend on typed interfaces at external boundaries.
- Validate all planner tool input; never expose a raw shell or filesystem tool.
- Make state transitions explicit and preserve idempotency and fencing.
- Use immutable revisions and non-destructive artifacts.
- Add tests at the lowest effective layer and regression tests for fixes.
- Do not add silent fallbacks for models, precision, storage, or security.
- Document configuration with a safe default in `.env.example`.
- Record third-party code, model, dataset, and fixture licenses separately.

## Commits and stages

Use Conventional Commits, for example:

```text
feat(s03): add typed lyrics patch tool
fix(s06): recheck queue before idle shutdown
docs: explain Colab credential isolation
```

Each commit should be a coherent, tested, revertible slice. Stage closure and
tag rules are defined in [ROADMAP.md](ROADMAP.md#34-commit-closure-và-tag).

## Pull requests

A pull request must include:

- the problem and intended user outcome;
- the relevant roadmap stage or reason it is out of roadmap;
- tests and exact verification commands;
- migration, rollback, security, privacy, and license impact;
- screenshots for visible UI changes;
- benchmark evidence for performance or quality claims;
- known limitations and follow-up work.

Maintainers may request an ADR when a change creates a lasting architectural
constraint. Approval means the change meets current project gates; it does not
guarantee inclusion in a particular release.

## Reporting security issues

Do not open a public issue for a suspected vulnerability. Follow
[SECURITY.md](SECURITY.md).

