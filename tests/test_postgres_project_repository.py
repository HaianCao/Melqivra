"""Real PostgreSQL project and immutable-revision contract tests."""

from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from agent_music.domain import DocumentType, ProjectId, RevisionId, UserId
from agent_music.postgres_project_repository import PostgresProjectRepository
from agent_music.project_repository import CorruptRevisionError, RevisionConflictError

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresProjectRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresProjectRepository(POSTGRES_DSN)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute(
                """
                TRUNCATE TABLE project_revision_index, project_revisions, projects
                RESTART IDENTITY
                """
            )
        self.alice = UserId("usr_alice")
        self.project = self.repository.create_project(
            owner_id=self.alice,
            title="PostgreSQL song",
            project_id=ProjectId("prj_postgres_song"),
            created_at=1.0,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_revision_ancestry_acceptance_and_stable_order_match_sqlite(self) -> None:
        first = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.LYRICS,
            payload={"line_ids": ["lin_one"], "texts": ["Một"]},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
            revision_id=RevisionId("rev_pg_one"),
            created_at=2.0,
        )
        second = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.LYRICS,
            payload={"line_ids": ["lin_one"], "texts": ["Một mới"]},
            expected_working_revision_id=first.id,
            actor="agent",
            reason="revision",
            revision_id=RevisionId("rev_pg_two"),
            created_at=2.0,
        )
        accepted = self.repository.accept_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.LYRICS,
            revision_id=first.id,
            expected_working_revision_id=second.id,
        )

        self.assertEqual(first.id, second.parent_revision_id)
        self.assertEqual(
            (first, second),
            self.repository.list_revisions(
                owner_id=self.alice,
                project_id=self.project.id,
                document_type=DocumentType.LYRICS,
            ),
        )
        self.assertEqual(["lin_one"], self.repository.revision_payload(second)["line_ids"])
        selection = accepted.selections[0]
        self.assertEqual(second.id, selection.working_revision_id)
        self.assertEqual(first.id, selection.accepted_revision_id)

    def test_cross_tenant_reads_and_mutations_do_not_reveal_project(self) -> None:
        bob = UserId("usr_bob")
        for operation in (
            lambda: self.repository.get_project(owner_id=bob, project_id=self.project.id),
            lambda: self.repository.get_index(owner_id=bob, project_id=self.project.id),
            lambda: self.repository.list_revisions(
                owner_id=bob,
                project_id=self.project.id,
                document_type=DocumentType.BRIEF,
            ),
            lambda: self.repository.commit_revision(
                owner_id=bob,
                project_id=self.project.id,
                document_type=DocumentType.BRIEF,
                payload={"concept": "hidden"},
                expected_working_revision_id=None,
                actor="bob",
                reason="unauthorized",
            ),
        ):
            with self.subTest(operation=operation), self.assertRaises(KeyError):
                operation()

    def test_competing_writers_allow_exactly_one_optimistic_commit(self) -> None:
        first = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BRIEF,
            payload={"concept": "base"},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        peer = PostgresProjectRepository(POSTGRES_DSN)
        barrier = threading.Barrier(2)

        def commit(repository: PostgresProjectRepository, revision_id: str) -> str | None:
            barrier.wait()
            try:
                revision = repository.commit_revision(
                    owner_id=self.alice,
                    project_id=self.project.id,
                    document_type=DocumentType.BRIEF,
                    payload={"concept": revision_id},
                    expected_working_revision_id=first.id,
                    actor="agent",
                    reason="competing writer",
                    revision_id=RevisionId(revision_id),
                )
            except RevisionConflictError:
                return None
            return str(revision.id)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(
                        lambda item: commit(*item),
                        (
                            (self.repository, "rev_competing_one"),
                            (peer, "rev_competing_two"),
                        ),
                    )
                )
        finally:
            peer.close()

        winners = [result for result in results if result is not None]
        self.assertEqual(1, len(winners))
        revisions = self.repository.list_revisions(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BRIEF,
        )
        self.assertEqual(2, len(revisions))
        self.assertEqual(first.id, revisions[1].parent_revision_id)

    def test_database_trigger_prevents_revision_mutation(self) -> None:
        revision = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.BLUEPRINT,
            payload={"sections": ["sec_intro"]},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        with (
            psycopg.connect(POSTGRES_DSN, autocommit=True) as connection,
            self.assertRaisesRegex(psycopg.errors.RaiseException, "immutable"),
        ):
            connection.execute(
                "UPDATE project_revisions SET actor = 'tampered' WHERE id = %s",
                (str(revision.id),),
            )

    def test_checksum_detects_database_corruption(self) -> None:
        revision = self.repository.commit_revision(
            owner_id=self.alice,
            project_id=self.project.id,
            document_type=DocumentType.GENERATION_PLAN,
            payload={"seed": 7},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
        )
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute(
                "ALTER TABLE project_revisions DISABLE TRIGGER immutable_project_revisions_update"
            )
            try:
                connection.execute(
                    "UPDATE project_revisions SET payload_json = '{\"seed\":8}' WHERE id = %s",
                    (str(revision.id),),
                )
            finally:
                connection.execute(
                    "ALTER TABLE project_revisions "
                    "ENABLE TRIGGER immutable_project_revisions_update"
                )
        with self.assertRaises(CorruptRevisionError):
            self.repository.get_revision(
                owner_id=self.alice,
                project_id=self.project.id,
                revision_id=revision.id,
            )


if __name__ == "__main__":
    unittest.main()
