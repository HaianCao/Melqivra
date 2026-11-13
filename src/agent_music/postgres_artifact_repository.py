"""PostgreSQL authority for immutable, tenant-scoped artifact metadata."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, cast

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_music.artifact_repository import (
    ArtifactBatchRecord,
    ArtifactPublicationConflictError,
    ArtifactPublicationState,
    ArtifactRecord,
    ArtifactSpec,
    CorruptArtifactMetadataError,
    artifact_batch_contract_sha256,
    validate_artifact_object_key,
    validate_artifact_specs,
)
from agent_music.domain import ArtifactId, ProjectId, RevisionId, UserId, validate_identifier
from agent_music.postgres_migrations import PostgresConnection, PostgresMigrator, PostgresPool


class PostgresArtifactRepository:
    """Persist publication metadata while PostgreSQL remains the authority."""

    def __init__(
        self,
        dsn: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 4,
        connection_timeout_seconds: float = 10.0,
        migrate: bool = True,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if min_pool_size < 1 or max_pool_size < min_pool_size:
            raise ValueError("PostgreSQL pool sizes are invalid")
        if connection_timeout_seconds <= 0:
            raise ValueError("PostgreSQL connection timeout must be positive")
        self._pool: PostgresPool = ConnectionPool(
            dsn,
            min_size=min_pool_size,
            max_size=max_pool_size,
            timeout=connection_timeout_seconds,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        try:
            self._pool.wait(timeout=connection_timeout_seconds)
            if migrate:
                PostgresMigrator(self._pool).apply()
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        """Close all pooled PostgreSQL connections."""

        self._pool.close()

    def reserve_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        artifacts: tuple[ArtifactSpec, ...],
        created_at: float | None = None,
    ) -> ArtifactBatchRecord:
        """Reserve an exact batch contract or return its idempotent prior record."""

        validate_identifier(str(owner_id))
        validate_identifier(str(project_id))
        validate_identifier(batch_id)
        validate_artifact_specs(artifacts)
        contract_sha256 = artifact_batch_contract_sha256(artifacts)
        timestamp = time.time() if created_at is None else created_at

        with self._pool.connection() as connection, connection.transaction():
            self._require_owned_project(
                connection,
                owner_id=owner_id,
                project_id=project_id,
            )
            self._require_project_revisions(connection, project_id=project_id, artifacts=artifacts)
            connection.execute(
                """
                INSERT INTO artifact_batches(
                    owner_id, project_id, batch_id, contract_sha256,
                    artifact_count, state, created_at, state_changed_at
                ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT(owner_id, project_id, batch_id) DO NOTHING
                """,
                (
                    str(owner_id),
                    str(project_id),
                    batch_id,
                    contract_sha256,
                    len(artifacts),
                    timestamp,
                    timestamp,
                ),
            )
            batch = connection.execute(
                """
                SELECT * FROM artifact_batches
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                FOR UPDATE
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchone()
            if batch is None:
                raise CorruptArtifactMetadataError("Reserved artifact batch disappeared")
            if str(batch["contract_sha256"]) != contract_sha256 or int(
                batch["artifact_count"]
            ) != len(artifacts):
                raise ArtifactPublicationConflictError(
                    "Artifact batch ID was reused for different content"
                )

            for ordinal, artifact in enumerate(artifacts):
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        owner_id, project_id, batch_id, ordinal, id, object_key,
                        media_type, size_bytes, sha256, source_revision_ids,
                        state, created_at, state_changed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'pending', %s, %s
                    )
                    ON CONFLICT(owner_id, id) DO NOTHING
                    """,
                    (
                        str(owner_id),
                        str(project_id),
                        batch_id,
                        ordinal,
                        str(artifact.artifact_id),
                        artifact.object_key,
                        artifact.media_type,
                        artifact.size_bytes,
                        artifact.sha256,
                        Jsonb([str(item) for item in artifact.source_revision_ids]),
                        timestamp,
                        timestamp,
                    ),
                )
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                ORDER BY ordinal
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchall()
            observed = tuple(self._row_to_artifact(row) for row in rows)
            self._verify_reserved_artifacts(observed, artifacts)
            return self._row_to_batch(batch, observed)

    def publish_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        published_at: float | None = None,
    ) -> ArtifactBatchRecord:
        """Atomically expose every member after object verification succeeds."""

        timestamp = time.time() if published_at is None else published_at
        with self._pool.connection() as connection, connection.transaction():
            batch = connection.execute(
                """
                SELECT * FROM artifact_batches
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                FOR UPDATE
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            state = ArtifactPublicationState(str(batch["state"]))
            current_rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                ORDER BY ordinal
                FOR UPDATE
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchall()
            current_artifacts = tuple(self._row_to_artifact(row) for row in current_rows)
            if len(current_artifacts) != int(batch["artifact_count"]) or any(
                item.state is not state for item in current_artifacts
            ):
                raise CorruptArtifactMetadataError("Artifact batch state is inconsistent")
            if state is ArtifactPublicationState.PENDING:
                connection.execute(
                    """
                    UPDATE artifacts
                    SET state = 'published', published_at = %s, state_changed_at = %s
                    WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                      AND state = 'pending'
                    """,
                    (timestamp, timestamp, str(owner_id), str(project_id), batch_id),
                )
                connection.execute(
                    """
                    UPDATE artifact_batches
                    SET state = 'published', published_at = %s, state_changed_at = %s
                    WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                      AND state = 'pending'
                    """,
                    (timestamp, timestamp, str(owner_id), str(project_id), batch_id),
                )
            elif state is not ArtifactPublicationState.PUBLISHED:
                raise ArtifactPublicationConflictError(
                    f"Artifact batch cannot publish from state {state.value}"
                )
            refreshed = connection.execute(
                """
                SELECT * FROM artifact_batches
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                ORDER BY ordinal
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchall()
        if refreshed is None:
            raise CorruptArtifactMetadataError("Published artifact batch disappeared")
        artifacts = tuple(self._row_to_artifact(row) for row in rows)
        if len(artifacts) != int(refreshed["artifact_count"]) or any(
            item.state is not ArtifactPublicationState.PUBLISHED for item in artifacts
        ):
            raise CorruptArtifactMetadataError("Published artifact batch is incomplete")
        return self._row_to_batch(refreshed, artifacts)

    def get_batch_for_owner(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
    ) -> ArtifactBatchRecord:
        """Return one exact owned batch without cross-tenant disclosure."""

        with self._pool.connection() as connection:
            batch = connection.execute(
                """
                SELECT * FROM artifact_batches
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                ORDER BY ordinal
                """,
                (str(owner_id), str(project_id), batch_id),
            ).fetchall()
        if batch is None:
            raise KeyError(batch_id)
        artifacts = tuple(self._row_to_artifact(row) for row in rows)
        batch_state = ArtifactPublicationState(str(batch["state"]))
        if len(artifacts) != int(batch["artifact_count"]) or any(
            item.state is not batch_state for item in artifacts
        ):
            raise CorruptArtifactMetadataError("Artifact batch metadata is incomplete")
        return self._row_to_batch(batch, artifacts)

    def get_published_for_owner(
        self,
        *,
        owner_id: UserId,
        artifact_id: ArtifactId,
    ) -> ArtifactRecord:
        """Return only visible owned metadata; all other cases look missing."""

        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT artifact.*
                FROM artifacts AS artifact
                JOIN artifact_batches AS batch
                  ON batch.owner_id = artifact.owner_id
                 AND batch.project_id = artifact.project_id
                 AND batch.batch_id = artifact.batch_id
                WHERE artifact.owner_id = %s AND artifact.id = %s
                  AND artifact.state = 'published' AND batch.state = 'published'
                """,
                (str(owner_id), str(artifact_id)),
            ).fetchone()
        if row is None:
            raise KeyError(str(artifact_id))
        return self._row_to_artifact(row)

    def mark_batch_corrupt(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        changed_at: float | None = None,
    ) -> ArtifactBatchRecord:
        """Atomically hide a pending or published batch after integrity failure."""

        return self._transition_batch(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            target=ArtifactPublicationState.CORRUPT,
            allowed={
                ArtifactPublicationState.PENDING,
                ArtifactPublicationState.PUBLISHED,
            },
            changed_at=time.time() if changed_at is None else changed_at,
        )

    def start_batch_deletion(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        changed_at: float | None = None,
    ) -> ArtifactBatchRecord:
        """Hide a batch before idempotent object deletion begins."""

        return self._transition_batch(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            target=ArtifactPublicationState.DELETING,
            allowed={
                ArtifactPublicationState.PENDING,
                ArtifactPublicationState.PUBLISHED,
                ArtifactPublicationState.CORRUPT,
            },
            idempotent={
                ArtifactPublicationState.DELETING,
                ArtifactPublicationState.DELETED,
            },
            changed_at=time.time() if changed_at is None else changed_at,
        )

    def finish_batch_deletion(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        deleted_at: float | None = None,
    ) -> ArtifactBatchRecord:
        """Commit the tombstone only after every immutable object was removed."""

        timestamp = time.time() if deleted_at is None else deleted_at
        return self._transition_batch(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            target=ArtifactPublicationState.DELETED,
            allowed={ArtifactPublicationState.DELETING},
            idempotent={ArtifactPublicationState.DELETED},
            changed_at=timestamp,
            deleted_at=timestamp,
        )

    def list_batches_for_reconciliation(
        self,
        *,
        states: tuple[ArtifactPublicationState, ...],
        changed_before: float,
        limit: int,
    ) -> tuple[ArtifactBatchRecord, ...]:
        """Return a bounded oldest-first internal lifecycle work set."""

        if not states or len(set(states)) != len(states):
            raise ValueError("Reconciliation states must be non-empty and unique")
        if not math.isfinite(changed_before):
            raise ValueError("Reconciliation cutoff must be finite")
        if limit < 1 or limit > 1_000:
            raise ValueError("Reconciliation limit must be between 1 and 1000")
        with self._pool.connection() as connection:
            batches = connection.execute(
                """
                SELECT * FROM artifact_batches
                WHERE state = ANY(%s) AND state_changed_at <= %s
                ORDER BY state_changed_at, owner_id, project_id, batch_id
                LIMIT %s
                """,
                ([state.value for state in states], changed_before, limit),
            ).fetchall()
            records: list[ArtifactBatchRecord] = []
            for batch in batches:
                rows = connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                    ORDER BY ordinal
                    """,
                    (batch["owner_id"], batch["project_id"], batch["batch_id"]),
                ).fetchall()
                artifacts = tuple(self._row_to_artifact(row) for row in rows)
                state = ArtifactPublicationState(str(batch["state"]))
                if len(artifacts) != int(batch["artifact_count"]) or any(
                    artifact.state is not state for artifact in artifacts
                ):
                    raise CorruptArtifactMetadataError(
                        "Artifact reconciliation metadata is inconsistent"
                    )
                records.append(self._row_to_batch(batch, artifacts))
        return tuple(records)

    def known_live_object_keys(self, keys: tuple[str, ...]) -> frozenset[str]:
        """Return only service metadata keys that have not reached a tombstone."""

        if not keys:
            return frozenset()
        if len(keys) > 1_000 or len(set(keys)) != len(keys):
            raise ValueError("Object-key lookup must contain 1-1000 unique keys")
        for key in keys:
            validate_artifact_object_key(key)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT object_key FROM artifacts
                WHERE object_key = ANY(%s) AND state <> 'deleted'
                """,
                (list(keys),),
            ).fetchall()
        return frozenset(str(row["object_key"]) for row in rows)

    def _transition_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        target: ArtifactPublicationState,
        allowed: set[ArtifactPublicationState],
        changed_at: float,
        idempotent: set[ArtifactPublicationState] | None = None,
        deleted_at: float | None = None,
    ) -> ArtifactBatchRecord:
        if not math.isfinite(changed_at):
            raise ValueError("Artifact state-change time must be finite")
        idempotent_states = idempotent or {target}
        with self._pool.connection() as connection, connection.transaction():
            batch, artifacts = self._load_locked_batch(
                connection,
                owner_id=owner_id,
                project_id=project_id,
                batch_id=batch_id,
            )
            current = ArtifactPublicationState(str(batch["state"]))
            if current in idempotent_states:
                return self._row_to_batch(batch, artifacts)
            if current not in allowed:
                raise ArtifactPublicationConflictError(
                    f"Artifact batch cannot transition from {current.value} to {target.value}"
                )
            if changed_at < float(batch["state_changed_at"]):
                raise ValueError("Artifact state-change time must not move backwards")
            connection.execute(
                """
                UPDATE artifacts
                SET state = %s, state_changed_at = %s,
                    deleted_at = COALESCE(%s, deleted_at)
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                  AND state = %s
                """,
                (
                    target.value,
                    changed_at,
                    deleted_at,
                    str(owner_id),
                    str(project_id),
                    batch_id,
                    current.value,
                ),
            )
            connection.execute(
                """
                UPDATE artifact_batches
                SET state = %s, state_changed_at = %s,
                    deleted_at = COALESCE(%s, deleted_at)
                WHERE owner_id = %s AND project_id = %s AND batch_id = %s
                  AND state = %s
                """,
                (
                    target.value,
                    changed_at,
                    deleted_at,
                    str(owner_id),
                    str(project_id),
                    batch_id,
                    current.value,
                ),
            )
            refreshed, refreshed_artifacts = self._load_locked_batch(
                connection,
                owner_id=owner_id,
                project_id=project_id,
                batch_id=batch_id,
            )
            if ArtifactPublicationState(str(refreshed["state"])) is not target or any(
                artifact.state is not target for artifact in refreshed_artifacts
            ):
                raise CorruptArtifactMetadataError("Artifact batch transition was incomplete")
            return self._row_to_batch(refreshed, refreshed_artifacts)

    def _load_locked_batch(
        self,
        connection: PostgresConnection,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
    ) -> tuple[Mapping[str, Any], tuple[ArtifactRecord, ...]]:
        batch = connection.execute(
            """
            SELECT * FROM artifact_batches
            WHERE owner_id = %s AND project_id = %s AND batch_id = %s
            FOR UPDATE
            """,
            (str(owner_id), str(project_id), batch_id),
        ).fetchone()
        if batch is None:
            raise KeyError(batch_id)
        rows = connection.execute(
            """
            SELECT * FROM artifacts
            WHERE owner_id = %s AND project_id = %s AND batch_id = %s
            ORDER BY ordinal
            FOR UPDATE
            """,
            (str(owner_id), str(project_id), batch_id),
        ).fetchall()
        artifacts = tuple(self._row_to_artifact(row) for row in rows)
        state = ArtifactPublicationState(str(batch["state"]))
        if len(artifacts) != int(batch["artifact_count"]) or any(
            artifact.state is not state for artifact in artifacts
        ):
            raise CorruptArtifactMetadataError("Artifact batch state is inconsistent")
        return batch, artifacts

    @staticmethod
    def _require_owned_project(
        connection: PostgresConnection,
        *,
        owner_id: UserId,
        project_id: ProjectId,
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM projects WHERE id = %s AND owner_id = %s FOR UPDATE",
            (str(project_id), str(owner_id)),
        ).fetchone()
        if row is None:
            raise KeyError(str(project_id))

    @staticmethod
    def _require_project_revisions(
        connection: PostgresConnection,
        *,
        project_id: ProjectId,
        artifacts: tuple[ArtifactSpec, ...],
    ) -> None:
        required = {
            str(revision_id)
            for artifact in artifacts
            for revision_id in artifact.source_revision_ids
        }
        if not required:
            return
        rows = connection.execute(
            """
            SELECT id FROM project_revisions
            WHERE project_id = %s AND id = ANY(%s)
            """,
            (str(project_id), list(required)),
        ).fetchall()
        if {str(row["id"]) for row in rows} != required:
            raise KeyError("Artifact source revision not found")

    @staticmethod
    def _verify_reserved_artifacts(
        observed: tuple[ArtifactRecord, ...],
        expected: tuple[ArtifactSpec, ...],
    ) -> None:
        if len(observed) != len(expected):
            raise ArtifactPublicationConflictError(
                "Artifact batch ID was reused with different members"
            )
        for ordinal, (record, spec) in enumerate(zip(observed, expected, strict=True)):
            if (
                record.ordinal != ordinal
                or record.artifact_id != spec.artifact_id
                or record.object_key != spec.object_key
                or record.media_type != spec.media_type
                or record.size_bytes != spec.size_bytes
                or record.sha256 != spec.sha256
                or record.source_revision_ids != spec.source_revision_ids
            ):
                raise ArtifactPublicationConflictError(
                    "Artifact batch ID was reused for different content"
                )

    @staticmethod
    def _row_to_artifact(row: Mapping[str, Any]) -> ArtifactRecord:
        source = row["source_revision_ids"]
        if not isinstance(source, list) or not all(isinstance(item, str) for item in source):
            raise CorruptArtifactMetadataError("Artifact source revisions are invalid")
        return ArtifactRecord(
            owner_id=UserId(str(row["owner_id"])),
            project_id=ProjectId(str(row["project_id"])),
            batch_id=str(row["batch_id"]),
            ordinal=int(row["ordinal"]),
            artifact_id=ArtifactId(str(row["id"])),
            object_key=str(row["object_key"]),
            media_type=str(row["media_type"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            source_revision_ids=tuple(RevisionId(item) for item in cast(list[str], source)),
            state=ArtifactPublicationState(str(row["state"])),
            created_at=float(row["created_at"]),
            state_changed_at=float(row["state_changed_at"]),
            published_at=(float(row["published_at"]) if row["published_at"] is not None else None),
            deleted_at=(float(row["deleted_at"]) if row["deleted_at"] is not None else None),
        )

    @staticmethod
    def _row_to_batch(
        row: Mapping[str, Any],
        artifacts: tuple[ArtifactRecord, ...],
    ) -> ArtifactBatchRecord:
        return ArtifactBatchRecord(
            owner_id=UserId(str(row["owner_id"])),
            project_id=ProjectId(str(row["project_id"])),
            batch_id=str(row["batch_id"]),
            contract_sha256=str(row["contract_sha256"]),
            state=ArtifactPublicationState(str(row["state"])),
            created_at=float(row["created_at"]),
            state_changed_at=float(row["state_changed_at"]),
            published_at=(float(row["published_at"]) if row["published_at"] is not None else None),
            deleted_at=(float(row["deleted_at"]) if row["deleted_at"] is not None else None),
            artifacts=artifacts,
        )
