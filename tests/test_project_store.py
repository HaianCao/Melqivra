"""S02 immutable project and revision tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_music.domain import (
    DocumentType,
    ProjectId,
    RevisionId,
    UserId,
    validate_identifier,
)
from agent_music.project_store import (
    CorruptRevisionError,
    RevisionConflictError,
    SqliteProjectRepository,
    content_checksum,
)


class SqliteProjectRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "projects.db"
        self.repository = SqliteProjectRepository(self.database)
        self.alice = UserId("usr_alice")
        self.project = self.repository.create_project(
            owner_id=self.alice,
            title="Stable song",
            project_id=ProjectId("prj_stable_song"),
            created_at=1.0,
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_revision_ancestry_and_stable_line_ids(self) -> None:
        line_ids = ["lin_verse_one", "lin_verse_two"]
        first = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.LYRICS,
            payload={"line_ids": line_ids, "texts": ["Một", "Hai"]},
            expected_working_revision_id=None,
            actor="user",
            reason="initial lyrics",
            revision_id=RevisionId("rev_lyrics_one"),
            created_at=2.0,
        )
        second = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.LYRICS,
            payload={"line_ids": line_ids, "texts": ["Một", "Hai mới"]},
            expected_working_revision_id=first.id,
            actor="agent",
            reason="revise unlocked line",
            revision_id=RevisionId("rev_lyrics_two"),
            created_at=3.0,
        )

        self.assertEqual(first.id, second.parent_revision_id)
        self.assertEqual(
            line_ids,
            self.repository.revision_payload(second)["line_ids"],
        )
        self.assertEqual(
            (first, second),
            self.repository.list_revisions(
                owner_id=self.alice,
                project_id=self.project.id,
                document_type=DocumentType.LYRICS,
            ),
        )

    def test_stale_concurrent_edit_is_rejected(self) -> None:
        first = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BRIEF,
            payload={"concept": "A"},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BRIEF,
            payload={"concept": "B"},
            expected_working_revision_id=first.id,
            actor="agent",
            reason="first writer",
        )
        with self.assertRaises(RevisionConflictError):
            self.repository.commit_revision(
                owner_id=self.alice,
                project_id=self.project.id,
                document_type=DocumentType.BRIEF,
                payload={"concept": "C"},
                expected_working_revision_id=first.id,
                actor="agent",
                reason="stale writer",
            )

    def test_acceptance_does_not_overwrite_history(self) -> None:
        first = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BLUEPRINT,
            payload={"sections": ["sec_intro"]},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        accepted = self.repository.accept_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BLUEPRINT,
            revision_id=first.id,
            expected_working_revision_id=first.id,
        )
        second = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BLUEPRINT,
            payload={"sections": ["sec_intro", "sec_chorus"]},
            expected_working_revision_id=first.id,
            actor="agent",
            reason="working branch",
        )
        selection = accepted.selections[0]
        self.assertEqual(first.id, selection.accepted_revision_id)
        current = self.repository.get_index(owner_id=self.alice, project_id=self.project.id)
        self.assertEqual(first.id, current.selections[0].accepted_revision_id)
        self.assertEqual(second.id, current.selections[0].working_revision_id)
        self.assertEqual({"sections": ["sec_intro"]}, self.repository.revision_payload(first))

    def test_cross_tenant_lookups_do_not_reveal_objects(self) -> None:
        bob = UserId("usr_bob")
        with self.assertRaises(KeyError):
            self.repository.get_project(owner_id=bob, project_id=self.project.id)
        with self.assertRaises(KeyError):
            self.repository.commit_revision(
                owner_id=bob,
                project_id=self.project.id,
                document_type=DocumentType.BRIEF,
                payload={"concept": "stolen"},
                expected_working_revision_id=None,
                actor="bob",
                reason="unauthorized",
            )

    def test_database_trigger_prevents_revision_mutation(self) -> None:
        revision = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BRIEF,
            payload={"concept": "immutable"},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE revisions SET payload_json = '{}' WHERE id = ?",
                    (str(revision.id),),
                )
        finally:
            connection.close()

    def test_checksum_detects_corruption(self) -> None:
        revision = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BRIEF,
            payload={"concept": "valid"},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER immutable_revisions_update")
            connection.execute(
                "UPDATE revisions SET payload_json = ? WHERE id = ?",
                ('{"concept":"corrupt"}', str(revision.id)),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(CorruptRevisionError):
            self.repository.get_revision(
                owner_id=self.alice,
                project_id=self.project.id,
                revision_id=revision.id,
            )

    def test_version_one_database_migrates_and_backfills_checksum(self) -> None:
        self.repository.close()
        self.database.unlink()
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    title TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE revisions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    document_type TEXT NOT NULL, parent_revision_id TEXT REFERENCES revisions(id),
                    payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE project_index (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    document_type TEXT NOT NULL,
                    working_revision_id TEXT REFERENCES revisions(id),
                    accepted_revision_id TEXT REFERENCES revisions(id),
                    PRIMARY KEY(project_id, document_type)
                );
                INSERT INTO projects VALUES ('prj_legacy', 'usr_alice', 'Legacy', 1.0);
                INSERT INTO revisions VALUES (
                    'rev_legacy', 'prj_legacy', 'brief', NULL,
                    '{"concept":"legacy"}', 'user', 2.0
                );
                PRAGMA user_version = 1;
                """
            )
        finally:
            connection.close()
        self.repository = SqliteProjectRepository(self.database)
        revision = self.repository.get_revision(
            owner_id=self.alice,
            project_id=ProjectId("prj_legacy"),
            revision_id=RevisionId("rev_legacy"),
        )
        self.assertEqual(content_checksum(revision.payload_json), revision.checksum)
        self.assertEqual("migrated", revision.reason)

    def test_identifier_validation_rejects_paths(self) -> None:
        for value in ("../escape", "x/y", "..", "C_drive"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_identifier(value)


if __name__ == "__main__":
    unittest.main()
