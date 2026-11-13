# Database migration policy

PostgreSQL migrations are immutable release artifacts. Their ordering, logical
identity, content checksum, and upgrade behavior are part of the service contract.

## File names

Use this format:

```text
NNNN_<action>_<scope>.sql
```

- `NNNN` is a zero-padded, contiguous version beginning at `0001`.
- `<action>` is one of `create`, `add`, `alter`, `backfill`, `rename`, or `drop`.
- `<scope>` names the durable concept affected, not the implementation ticket.
- Use lowercase ASCII snake case only.

Examples:

```text
0001_create_gpu_job_queue.sql
0003_add_gpu_job_attempt_history.sql
0004_create_project_revision_store.sql
0005_create_approval_audit_authority.sql
0008_add_artifact_reconciliation_state.sql
```

The version prefix is required. It provides deterministic ordering and is
independent of wall-clock timestamps, developer machines, or branch names.

## Immutability and compatibility

- Never edit, delete, reorder, or reuse an applied migration version.
- Never change an applied migration's logical `name` in the catalog.
- Line endings are normalized before calculating the stored SHA-256 checksum.
- Correct an old schema by appending a new forward migration.
- Renaming a packaged resource is allowed only while preserving its version,
  logical name, SQL bytes after line-ending normalization, and canonical hash.
- Every migration must be packaged in the wheel and applied under the shared
  PostgreSQL advisory transaction lock.
- Schema changes must document backup, restore, rollback, and compatibility
  implications before release.

## Verification

The CPU-safe migration catalog tests enforce contiguous versions, unique logical
names/resources, the filename grammar, packaged resource presence, and canonical
hashes. The real PostgreSQL CI contract applies the complete catalog twice and
exercises upgrade, contention, authorization, and corruption behavior.
