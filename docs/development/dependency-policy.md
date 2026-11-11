# Dependency policy

## Principles

- Runtime dependencies must directly serve a product requirement.
- Development dependencies must enforce a documented quality or security gate.
- `uv.lock` is committed and CI installs with `--locked`/`--frozen` semantics.
- Production and development ranges stay bounded by the next incompatible major.
- Model weights and datasets are not Python dependencies and require separate
  provenance and license records.

## Adding or upgrading a dependency

1. Record purpose, license, maintenance state, and alternatives in the PR.
2. Avoid packages that duplicate standard-library or existing capability.
3. Update `pyproject.toml`, regenerate `uv.lock`, and inspect the lock diff.
4. Run format, lint, mypy, tests/coverage, build, and `pip-audit`.
5. Test upgrade and rollback when persistence or artifact formats are affected.
6. Update notices/model-license inventory when required.

Dependabot proposes weekly Python and GitHub Actions updates. Automated proposals
must pass the same review and gates as human changes. GitHub Actions are pinned
to immutable commit SHAs with a version comment.

## Vulnerabilities

Known vulnerabilities are triaged by reachability, exploitability, affected
configuration, and available mitigation. A temporary exception requires an
owner, rationale, compensating control, expiry date, and private handling when
disclosure would increase risk. Critical issues block a release.

