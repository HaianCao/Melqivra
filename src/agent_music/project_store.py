"""SQLite repository for immutable song-project revisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

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

SCHEMA_VERSION = 2


class RevisionConflictError(RuntimeError):
    """The caller edited a stale working revision."""


class CorruptRevisionError(RuntimeError):
    """Stored revision content no longer matches its checksum."""


class ProjectRepository(Protocol):
    """Tenant-scoped authority for projects and immutable document revisions."""

    def close(self) -> None: ...

    def create_project(
        self,
        *,
        owner_id: UserId,
        title: str,
        project_id: ProjectId | None = None,
        created_at: float | None = None,
    ) -> Project: ...

    def get_project(self, *, owner_id: UserId, project_id: ProjectId) -> Project: ...

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
    ) -> Revision: ...

    def accept_revision(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        document_type: DocumentType,
        revision_id: RevisionId,
        expected_working_revision_id: RevisionId,
    ) -> ProjectIndex: ...

    def get_revision(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        revision_id: RevisionId,
    ) -> Revision: ...

    def revision_payload(self, revision: Revision) -> JsonObject: ...

    def list_revisions(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        document_type: DocumentType,
    ) -> tuple[Revision, ...]: ...

    def get_index(self, *, owner_id: UserId, project_id: ProjectId) -> ProjectIndex: ...


def canonical_json(payload: Mapping[str, JsonValue]) -> str:
    """Serialize a finite JSON object deterministically."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("Revision payload must be an object")
    return encoded


def content_checksum(payload_json: str) -> str:
    """Return the revision content checksum."""

    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class SqliteProjectRepository:
    """Tenant-scoped project and immutable revision repository."""

    def __init__(self, database_path: Path) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            database_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported project schema version: {version}")
            if version == 0:
                self._apply_v1()
                version = 1
            if version == 1:
                self._apply_v2()

    def _apply_v1(self) -> None:
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE revisions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                document_type TEXT NOT NULL,
                parent_revision_id TEXT REFERENCES revisions(id),
                payload_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE project_index (
                project_id TEXT NOT NULL REFERENCES projects(id),
                document_type TEXT NOT NULL,
                working_revision_id TEXT REFERENCES revisions(id),
                accepted_revision_id TEXT REFERENCES revisions(id),
                PRIMARY KEY(project_id, document_type)
            );
            CREATE INDEX ix_revisions_project_type_created
                ON revisions(project_id, document_type, created_at);
            PRAGMA user_version = 1;
            COMMIT;
            """
        )

    def _apply_v2(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "ALTER TABLE revisions ADD COLUMN reason TEXT NOT NULL DEFAULT 'migrated'"
            )
            self._connection.execute(
                "ALTER TABLE revisions ADD COLUMN checksum TEXT NOT NULL DEFAULT ''"
            )
            rows = self._connection.execute("SELECT id, payload_json FROM revisions").fetchall()
            for row in rows:
                self._connection.execute(
                    "UPDATE revisions SET checksum = ? WHERE id = ?",
                    (content_checksum(str(row["payload_json"])), str(row["id"])),
                )
            self._connection.execute(
                """
                CREATE TRIGGER immutable_revisions_update
                BEFORE UPDATE ON revisions
                BEGIN
                    SELECT RAISE(ABORT, 'revisions are immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER immutable_revisions_delete
                BEFORE DELETE ON revisions
                BEGIN
                    SELECT RAISE(ABORT, 'revisions are immutable');
                END
                """
            )
            self._connection.execute("PRAGMA user_version = 2")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        """Close the repository connection."""

        with self._lock:
            self._connection.close()

    def create_project(
        self,
        *,
        owner_id: UserId,
        title: str,
        project_id: ProjectId | None = None,
        created_at: float | None = None,
    ) -> Project:
        """Create a tenant-owned project root."""

        validate_identifier(str(owner_id))
        if not title.strip():
            raise ValueError("Project title must not be empty")
        resolved_id = project_id or ProjectId(new_identifier("prj"))
        validate_identifier(str(resolved_id))
        timestamp = time.time() if created_at is None else created_at
        with self._lock:
            self._connection.execute(
                "INSERT INTO projects(id, owner_id, title, created_at) VALUES (?, ?, ?, ?)",
                (str(resolved_id), str(owner_id), title.strip(), timestamp),
            )
        return Project(resolved_id, owner_id, title.strip(), timestamp)

    def get_project(self, *, owner_id: UserId, project_id: ProjectId) -> Project:
        """Return an owned project without leaking cross-tenant existence."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
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
        """Append a revision if the caller's working pointer is current."""

        self.get_project(owner_id=owner_id, project_id=project_id)
        if not actor.strip() or not reason.strip():
            raise ValueError("Revision actor and reason are required")
        payload_json = canonical_json(payload)
        checksum = content_checksum(payload_json)
        resolved_id = revision_id or RevisionId(new_identifier("rev"))
        validate_identifier(str(resolved_id))
        timestamp = time.time() if created_at is None else created_at

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT working_revision_id FROM project_index
                    WHERE project_id = ? AND document_type = ?
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
                self._connection.execute(
                    """
                    INSERT INTO revisions(
                        id, project_id, document_type, parent_revision_id,
                        payload_json, actor, created_at, reason, checksum
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(resolved_id),
                        str(project_id),
                        document_type.value,
                        str(current) if current else None,
                        payload_json,
                        actor.strip(),
                        timestamp,
                        reason.strip(),
                        checksum,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO project_index(
                        project_id, document_type, working_revision_id, accepted_revision_id
                    ) VALUES (?, ?, ?, NULL)
                    ON CONFLICT(project_id, document_type) DO UPDATE SET
                        working_revision_id = excluded.working_revision_id
                    """,
                    (str(project_id), document_type.value, str(resolved_id)),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
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
        """Move only the accepted pointer after an optimistic working check."""

        self.get_project(owner_id=owner_id, project_id=project_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT working_revision_id FROM project_index
                    WHERE project_id = ? AND document_type = ?
                    """,
                    (str(project_id), document_type.value),
                ).fetchone()
                current = str(row["working_revision_id"]) if row else None
                if current != str(expected_working_revision_id):
                    raise RevisionConflictError("Working revision changed before acceptance")
                target = self._connection.execute(
                    """
                    SELECT 1 FROM revisions
                    WHERE id = ? AND project_id = ? AND document_type = ?
                    """,
                    (str(revision_id), str(project_id), document_type.value),
                ).fetchone()
                if target is None:
                    raise KeyError(str(revision_id))
                self._connection.execute(
                    """
                    UPDATE project_index SET accepted_revision_id = ?
                    WHERE project_id = ? AND document_type = ?
                    """,
                    (str(revision_id), str(project_id), document_type.value),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_index(owner_id=owner_id, project_id=project_id)

    def get_revision(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        revision_id: RevisionId,
    ) -> Revision:
        """Load and checksum-verify one tenant-owned revision."""

        self.get_project(owner_id=owner_id, project_id=project_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM revisions WHERE id = ? AND project_id = ?",
                (str(revision_id), str(project_id)),
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
        """List one document lineage in creation order."""

        self.get_project(owner_id=owner_id, project_id=project_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM revisions
                WHERE project_id = ? AND document_type = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (str(project_id), document_type.value),
            ).fetchall()
        revisions = tuple(self._row_to_revision(row) for row in rows)
        for revision in revisions:
            if content_checksum(revision.payload_json) != revision.checksum:
                raise CorruptRevisionError(str(revision.id))
        return revisions

    def get_index(self, *, owner_id: UserId, project_id: ProjectId) -> ProjectIndex:
        """Return working and accepted pointers for every document family."""

        self.get_project(owner_id=owner_id, project_id=project_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM project_index
                WHERE project_id = ? ORDER BY document_type
                """,
                (str(project_id),),
            ).fetchall()
        selections = tuple(
            RevisionSelection(
                document_type=DocumentType(str(row["document_type"])),
                working_revision_id=(
                    RevisionId(str(row["working_revision_id"]))
                    if row["working_revision_id"] is not None
                    else None
                ),
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
    def _row_to_project(row: sqlite3.Row) -> Project:
        return Project(
            id=ProjectId(str(row["id"])),
            owner_id=UserId(str(row["owner_id"])),
            title=str(row["title"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> Revision:
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
