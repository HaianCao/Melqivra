"""PostgreSQL authority for tenant-scoped projects and immutable revisions."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, cast

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent_music.domain import (
    DocumentType,
    JsonObject,
    JsonValue,
    Project,
    ProjectId,
    ProjectIndex,
    Revision,
    RevisionId,
    RevisionSelection,
    UserId,
    new_identifier,
    validate_identifier,
)
from agent_music.postgres_migrations import PostgresConnection, PostgresMigrator, PostgresPool
from agent_music.project_repository import (
    CorruptRevisionError,
    RevisionConflictError,
    canonical_json,
    content_checksum,
)


class PostgresProjectRepository:
    """PostgreSQL project repository preserving the SQLite domain contract."""

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

    def create_project(
        self,
        *,
        owner_id: UserId,
        title: str,
        project_id: ProjectId | None = None,
        created_at: float | None = None,
    ) -> Project:
        """Create one tenant-owned project root."""

        validate_identifier(str(owner_id))
        if not title.strip():
            raise ValueError("Project title must not be empty")
        resolved_id = project_id or ProjectId(new_identifier("prj"))
        validate_identifier(str(resolved_id))
        timestamp = time.time() if created_at is None else created_at
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, owner_id, title, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (str(resolved_id), str(owner_id), title.strip(), timestamp),
            )
        return Project(resolved_id, owner_id, title.strip(), timestamp)

    def get_project(self, *, owner_id: UserId, project_id: ProjectId) -> Project:
        """Return an owned project without revealing cross-tenant existence."""

        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = %s AND owner_id = %s",
                (str(project_id), str(owner_id)),
            ).fetchone()
        if row is None:
            raise KeyError(str(project_id))
        return self._row_to_project(row)

    def commit_revision(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        document_type: DocumentType,
        payload: Mapping[str, JsonValue],
        expected_working_revision_id: RevisionId | None,
        actor: str,
        reason: str,
        revision_id: RevisionId | None = None,
        created_at: float | None = None,
    ) -> Revision:
        """Append a revision under a tenant lock and optimistic pointer check."""

        if not actor.strip() or not reason.strip():
            raise ValueError("Revision actor and reason are required")
        payload_json = canonical_json(payload)
        checksum = content_checksum(payload_json)
        resolved_id = revision_id or RevisionId(new_identifier("rev"))
        validate_identifier(str(resolved_id))
        timestamp = time.time() if created_at is None else created_at

        with self._pool.connection() as connection, connection.transaction():
            self._require_owned_project(
                connection,
                owner_id=owner_id,
                project_id=project_id,
                for_update=True,
            )
            row = connection.execute(
                """
                SELECT working_revision_id
                FROM project_revision_index
                WHERE project_id = %s AND document_type = %s
                FOR UPDATE
                """,
                (str(project_id), document_type.value),
            ).fetchone()
            current = (
                RevisionId(str(row["working_revision_id"]))
                if row is not None and row["working_revision_id"] is not None
                else None
            )
            if current != expected_working_revision_id:
                raise RevisionConflictError(
                    f"Working revision changed: expected {expected_working_revision_id}, "
                    f"found {current}"
                )
            connection.execute(
                """
                INSERT INTO project_revisions(
                    id, project_id, document_type, parent_revision_id,
                    payload_json, checksum, actor, reason, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(resolved_id),
                    str(project_id),
                    document_type.value,
                    str(current) if current else None,
                    payload_json,
                    checksum,
                    actor.strip(),
                    reason.strip(),
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO project_revision_index(
                    project_id, document_type, working_revision_id, accepted_revision_id
                ) VALUES (%s, %s, %s, NULL)
                ON CONFLICT(project_id, document_type) DO UPDATE SET
                    working_revision_id = EXCLUDED.working_revision_id
                """,
                (str(project_id), document_type.value, str(resolved_id)),
            )
        return self.get_revision(
            owner_id=owner_id,
            project_id=project_id,
            revision_id=resolved_id,
        )

    def accept_revision(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        document_type: DocumentType,
        revision_id: RevisionId,
        expected_working_revision_id: RevisionId,
    ) -> ProjectIndex:
        """Move only the accepted pointer after a tenant-scoped optimistic check."""

        with self._pool.connection() as connection, connection.transaction():
            self._require_owned_project(
                connection,
                owner_id=owner_id,
                project_id=project_id,
                for_update=True,
            )
            row = connection.execute(
                """
                SELECT working_revision_id
                FROM project_revision_index
                WHERE project_id = %s AND document_type = %s
                FOR UPDATE
                """,
                (str(project_id), document_type.value),
            ).fetchone()
            current = str(row["working_revision_id"]) if row is not None else None
            if current != str(expected_working_revision_id):
                raise RevisionConflictError("Working revision changed before acceptance")
            target = connection.execute(
                """
                SELECT 1 FROM project_revisions
                WHERE id = %s AND project_id = %s AND document_type = %s
                """,
                (str(revision_id), str(project_id), document_type.value),
            ).fetchone()
            if target is None:
                raise KeyError(str(revision_id))
            connection.execute(
                """
                UPDATE project_revision_index
                SET accepted_revision_id = %s
                WHERE project_id = %s AND document_type = %s
                """,
                (str(revision_id), str(project_id), document_type.value),
            )
        return self.get_index(owner_id=owner_id, project_id=project_id)

    def get_revision(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        revision_id: RevisionId,
    ) -> Revision:
        """Load and checksum-verify one tenant-owned revision."""

        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT revision.*
                FROM project_revisions AS revision
                JOIN projects AS project ON project.id = revision.project_id
                WHERE revision.id = %s AND revision.project_id = %s
                  AND project.owner_id = %s
                """,
                (str(revision_id), str(project_id), str(owner_id)),
            ).fetchone()
        if row is None:
            raise KeyError(str(revision_id))
        revision = self._row_to_revision(row)
        if content_checksum(revision.payload_json) != revision.checksum:
            raise CorruptRevisionError(str(revision.id))
        return revision

    def revision_payload(self, revision: Revision) -> JsonObject:
        """Decode a verified revision to a fresh mutable object."""

        if content_checksum(revision.payload_json) != revision.checksum:
            raise CorruptRevisionError(str(revision.id))
        payload = json.loads(revision.payload_json)
        if not isinstance(payload, dict):
            raise CorruptRevisionError(str(revision.id))
        return cast(JsonObject, payload)

    def list_revisions(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        document_type: DocumentType,
    ) -> tuple[Revision, ...]:
        """List one tenant-owned document lineage in stable creation order."""

        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT revision.*
                FROM project_revisions AS revision
                JOIN projects AS project ON project.id = revision.project_id
                WHERE revision.project_id = %s AND revision.document_type = %s
                  AND project.owner_id = %s
                ORDER BY revision.created_at ASC, revision.revision_order ASC
                """,
                (str(project_id), document_type.value, str(owner_id)),
            ).fetchall()
        if not rows:
            self.get_project(owner_id=owner_id, project_id=project_id)
        revisions = tuple(self._row_to_revision(row) for row in rows)
        for revision in revisions:
            if content_checksum(revision.payload_json) != revision.checksum:
                raise CorruptRevisionError(str(revision.id))
        return revisions

    def get_index(self, *, owner_id: UserId, project_id: ProjectId) -> ProjectIndex:
        """Return working and accepted pointers for one tenant-owned project."""

        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT project_index.*
                FROM project_revision_index AS project_index
                JOIN projects AS project ON project.id = project_index.project_id
                WHERE project_index.project_id = %s AND project.owner_id = %s
                ORDER BY project_index.document_type
                """,
                (str(project_id), str(owner_id)),
            ).fetchall()
        if not rows:
            self.get_project(owner_id=owner_id, project_id=project_id)
        selections = tuple(
            RevisionSelection(
                document_type=DocumentType(str(row["document_type"])),
                working_revision_id=RevisionId(str(row["working_revision_id"])),
                accepted_revision_id=(
                    RevisionId(str(row["accepted_revision_id"]))
                    if row["accepted_revision_id"] is not None
                    else None
                ),
            )
            for row in rows
        )
        return ProjectIndex(project_id=project_id, selections=selections)

    @staticmethod
    def _require_owned_project(
        connection: PostgresConnection,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        for_update: bool,
    ) -> dict[str, Any]:
        lock = " FOR UPDATE" if for_update else ""
        row = connection.execute(
            "SELECT * FROM projects WHERE id = %s AND owner_id = %s" + lock,
            (str(project_id), str(owner_id)),
        ).fetchone()
        if row is None:
            raise KeyError(str(project_id))
        return row

    @staticmethod
    def _row_to_project(row: Mapping[str, Any]) -> Project:
        return Project(
            id=ProjectId(str(row["id"])),
            owner_id=UserId(str(row["owner_id"])),
            title=str(row["title"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _row_to_revision(row: Mapping[str, Any]) -> Revision:
        return Revision(
            id=RevisionId(str(row["id"])),
            project_id=ProjectId(str(row["project_id"])),
            document_type=DocumentType(str(row["document_type"])),
            parent_revision_id=(
                RevisionId(str(row["parent_revision_id"]))
                if row["parent_revision_id"] is not None
                else None
            ),
            payload_json=str(row["payload_json"]),
            checksum=str(row["checksum"]),
            actor=str(row["actor"]),
            reason=str(row["reason"]),
            created_at=float(row["created_at"]),
        )
