# S02 evidence — Versioned song-project domain

Date: 2026-11-12

Merged implementation checkpoint:
`bcc5d7c feat(s02): add immutable project revision domain (#11)`.

Protected pull-request CI passed all eight required matrix jobs on PR #11 before
merge (Ubuntu/Windows on Python 3.11 and 3.13, with quality and test jobs).

## Environment

- Windows x86_64
- Python 3.11.17 through the locked `uv` environment
- SQLite with WAL and foreign-key enforcement
- CPU-only; no GPU or external provider was used

## Proven behavior

- Framework-independent typed contracts cover users, projects, structured song
  documents, stable sections/lines, revisions, approvals, jobs, takes, artifacts,
  and generation manifests.
- SQLite revisions are append-only, checksummed, tenant-scoped, and protected by
  optimistic concurrency plus database immutability triggers.
- Working and accepted pointers are independent, so accepting a revision never
  overwrites history and later working edits do not move the accepted pointer.
- The v1-to-v2 migration backfills revision checksums and installs immutable
  update/delete guards.
- Artifact writes use a same-directory temporary file, flush and `fsync`, atomic
  replacement, safe project-relative paths, MIME validation, and read-time size
  and SHA-256 verification.
- Portable archives use a deterministic manifest and reject unexpected ZIP
  entries, excessive manifests, invalid ancestry, and checksum mismatches.
  Round-trip import preserves project/revision IDs, timestamps, history,
  accepted/working pointers, payloads, and checksums.

## Tests and gate

The complete repository gate passed locally:

```text
uv sync --locked --extra dev                         PASS
ruff format --check src tests scripts                PASS
ruff check src tests scripts                         PASS
mypy                                                  PASS (22 source files)
coverage run -m unittest discover -s tests -v         PASS (69 tests)
coverage report                                       PASS (81% critical paths)
uv build                                               PASS (wheel and sdist)
pip-audit                                              PASS (no known vulnerabilities)
```

S02-specific tests cover stable IDs and ancestry, stale concurrent writers,
accepted-history preservation, cross-tenant access, database immutability,
revision corruption, schema migration, unsafe identifiers, atomic-write
interruption, artifact corruption/cross-project reads, deterministic golden
archive output, and archive traversal/unexpected-entry rejection.

Golden fixture: `tests/fixtures/s02_project_archive.json`.

## Limitations

- The reference repository remains SQLite and a single control-plane process;
  production PostgreSQL/object-storage topology is intentionally deferred to
  S07.
- The portable archive currently contains structured project history. Generated
  media packaging will be added with the first audio vertical slice, after a
  production backend is selected.
