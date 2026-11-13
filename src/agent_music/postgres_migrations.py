"""Ordered, immutable PostgreSQL migration catalog and executor."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

_MIGRATION_LOCK_ID = 1_296_382_033
_MIGRATION_FILENAME = re.compile(
    r"^(?P<version>[0-9]{4})_"
    r"(?P<action>create|add|alter|backfill|drop|rename)_"
    r"[a-z0-9]+(?:_[a-z0-9]+)*[.]sql$"
)

PostgresConnection = Connection[dict[str, Any]]
PostgresPool = ConnectionPool[PostgresConnection]


class PostgresMigrationError(RuntimeError):
    """A tracked PostgreSQL migration is missing, malformed, or no longer immutable."""


@dataclass(frozen=True, slots=True)
class PostgresMigration:
    """One ordered, checksummed package migration."""

    version: int
    name: str
    resource: str

    def read(self) -> tuple[str, str]:
        """Return migration SQL and its line-ending-independent SHA-256."""

        content = resources.files("agent_music").joinpath(self.resource).read_text(encoding="utf-8")
        canonical = content.replace("\r\n", "\n").replace("\r", "\n")
        encoded = canonical.encode("utf-8")
        return canonical, hashlib.sha256(encoded).hexdigest()


POSTGRES_MIGRATIONS = (
    PostgresMigration(
        version=1,
        name="job_queue",
        resource="migrations/postgresql/0001_create_gpu_job_queue.sql",
    ),
    PostgresMigration(
        version=2,
        name="job_leases",
        resource="migrations/postgresql/0002_add_gpu_job_leases.sql",
    ),
    PostgresMigration(
        version=3,
        name="job_attempt_history",
        resource="migrations/postgresql/0003_add_gpu_job_attempt_history.sql",
    ),
    PostgresMigration(
        version=4,
        name="project_revisions",
        resource="migrations/postgresql/0004_create_project_revision_store.sql",
    ),
    PostgresMigration(
        version=5,
        name="approval_audit_authority",
        resource="migrations/postgresql/0005_create_approval_audit_authority.sql",
    ),
    PostgresMigration(
        version=6,
        name="job_event_stream",
        resource="migrations/postgresql/0006_create_gpu_job_event_stream.sql",
    ),
    PostgresMigration(
        version=7,
        name="artifact_publication_authority",
        resource="migrations/postgresql/0007_create_artifact_publication_authority.sql",
    ),
)


def validate_postgres_migration_catalog(
    migrations: tuple[PostgresMigration, ...] = POSTGRES_MIGRATIONS,
) -> None:
    """Reject gaps, duplicates, or non-conforming packaged migration names."""

    versions = tuple(migration.version for migration in migrations)
    if versions != tuple(range(1, len(migrations) + 1)):
        raise PostgresMigrationError("PostgreSQL migration versions must be contiguous from 1")
    names = tuple(migration.name for migration in migrations)
    resources_seen = tuple(migration.resource for migration in migrations)
    if len(set(names)) != len(names) or len(set(resources_seen)) != len(resources_seen):
        raise PostgresMigrationError("PostgreSQL migration names and resources must be unique")
    for migration in migrations:
        resource = PurePosixPath(migration.resource)
        if resource.parent.as_posix() != "migrations/postgresql":
            raise PostgresMigrationError("PostgreSQL migrations must use the packaged catalog")
        match = _MIGRATION_FILENAME.fullmatch(resource.name)
        if match is None or int(match.group("version")) != migration.version:
            raise PostgresMigrationError(
                f"PostgreSQL migration {migration.version} has a non-conforming resource name"
            )


validate_postgres_migration_catalog()


class PostgresMigrator:
    """Apply immutable package migrations under one PostgreSQL advisory lock."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def apply(self) -> tuple[int, ...]:
        """Apply pending migrations and return their versions."""

        applied: list[int] = []
        with self._pool.connection() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            existing = {int(row["version"]): row for row in rows}
            known_versions = {migration.version for migration in POSTGRES_MIGRATIONS}
            unknown = set(existing) - known_versions
            if unknown:
                raise PostgresMigrationError(
                    f"Database has unknown migration versions: {sorted(unknown)}"
                )
            for migration in POSTGRES_MIGRATIONS:
                sql, checksum = migration.read()
                prior = existing.get(migration.version)
                if prior is not None:
                    if prior["name"] != migration.name or prior["checksum"] != checksum:
                        raise PostgresMigrationError(
                            f"PostgreSQL migration {migration.version} checksum mismatch"
                        )
                    continue
                connection.execute(sql, prepare=False)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, checksum),
                )
                applied.append(migration.version)
        return tuple(applied)
