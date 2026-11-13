# PostgreSQL and object-storage backup/restore

S07 treats PostgreSQL as the durable metadata authority and S3-compatible
storage as immutable byte storage. A valid service backup therefore contains
both authorities and must restore them as one checked contract.

## Archive format

`melqivra backup-service` creates one new directory atomically. It contains:

- `database.dump`: a compressed PostgreSQL custom-format archive created by
  `pg_dump` with ownership and access-control statements excluded;
- `objects/<prefix>/<sha256>.bin`: content-addressed immutable artifact bytes;
- `manifest.json`: canonical JSON mapping PostgreSQL-authorized object keys to
  media type, byte length, checksum, database state, and blob path;
- `manifest.sha256`: the SHA-256 of the exact canonical manifest bytes.

The archive is confidential. The database dump can contain project documents,
job payloads, and operational metadata; the manifest contains internal object
keys. Store it in encrypted, access-controlled backup storage. Do not commit it,
publish it as CI output, or place it in a browser-accessible bucket.

Published artifact state is held stable with a PostgreSQL advisory lock while
the database snapshot and object inventory are captured. Other PostgreSQL
transactions may continue; `pg_dump` supplies their normal transactionally
consistent point-in-time snapshot. A complete pending object is included. An
incomplete pending object is counted and omitted because it is not publicly
visible and will converge through artifact reconciliation after restore.

## Create a backup

Configure the same PostgreSQL and S3 values used by the service, then run:

```shell
uv run melqivra backup-service --output /secure/backups/melqivra-YYYYMMDD
```

The default archive mode uses installed `pg_dump`/`pg_restore` executables. A
digest-pinned PostgreSQL client container is available for hosts that operate
the service through Docker:

```dotenv
SERVICE_ARCHIVE_MODE=docker
DOCKER_EXECUTABLE=docker
POSTGRES_ARCHIVE_DOCKER_IMAGE=postgres:17-alpine@sha256:d3244e8ce3f165ee3c93d9c4a4b1d0fe48b3a89e2287b717fdcbbde92d405065
```

The Docker client uses host networking, a read-only mount during restore, and a
short-lived private environment file. Host, user, and password stay out of
process arguments; only the non-secret target database name is passed to
`pg_restore`. The temporary client container is removed on failure or timeout.

All operations are bounded:

```dotenv
SERVICE_ARCHIVE_TIMEOUT_SECONDS=3600
SERVICE_ARCHIVE_MAX_DATABASE_BYTES=1099511627776
SERVICE_ARCHIVE_MAX_OBJECTS=100000
SERVICE_ARCHIVE_MAX_TOTAL_OBJECT_BYTES=1099511627776
SERVICE_ARCHIVE_OBJECT_PAGE_SIZE=1000
```

Set bounds for the deployment's documented capacity and alert before they are
reached. Raising a bound is an operational capacity decision, not a workaround
for an unexplained archive failure.

## Restore

Restore only into a newly created empty PostgreSQL database and an empty S3
artifact prefix:

```shell
uv run melqivra restore-service --input /secure/backups/melqivra-YYYYMMDD
```

The restore sequence is intentionally asymmetric:

1. verify the manifest checksum, canonical schema, database dump checksum, every
   content-addressed blob, configured limits, and safe relative paths;
2. refuse a PostgreSQL target containing any public relation;
3. refuse unrelated or changed objects in the target artifact prefix, while
   allowing exact immutable objects from an interrupted prior restore;
4. upload and independently verify all immutable bytes;
5. restore PostgreSQL in one `pg_restore --single-transaction` operation;
6. verify the migration version, every published metadata contract, and every
   restored object's length and SHA-256.

Uploading bytes before metadata means an interrupted database restore can leave
only harmless unreferenced immutable objects. It cannot expose a published
PostgreSQL record whose bytes have not been restored. The operation is safe to
retry while PostgreSQL remains empty and existing object bytes are exact.

After a successful restore, run `melqivra doctor`, one bounded
`melqivra reconcile-artifacts` pass, tenant-isolation checks, and an authorized
artifact download before directing production traffic to the target.

## Rotation and recovery exercises

- Keep multiple independently encrypted restore points according to the
  deployment's recovery-point and legal-retention policy.
- Test restore into fresh infrastructure on a schedule; a successful backup
  command alone is not recovery evidence.
- Record only backup IDs, timestamps, counts, sizes, manifest checksums, tool
  versions, and pass/fail state in operator logs. Never log DSNs, credentials,
  object keys, job payloads, project text, or artifact bytes.
- Delete expired archives through the backup storage provider's reviewed
  retention mechanism. The application deliberately does not delete external
  backup media.
