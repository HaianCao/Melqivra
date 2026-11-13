"""Real PostgreSQL artifact-publication metadata contract tests."""

from __future__ import annotations

import hashlib
import os
import unittest

import psycopg

from agent_music.artifact_repository import (
    ArtifactPublicationConflictError,
    ArtifactPublicationState,
    ArtifactSpec,
)
from agent_music.domain import (
    ArtifactId,
    DocumentType,
    ProjectId,
    RevisionId,
    UserId,
)
from agent_music.postgres_artifact_repository import PostgresArtifactRepository
from agent_music.postgres_project_repository import PostgresProjectRepository

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresArtifactRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresArtifactRepository(POSTGRES_DSN)
        self.projects = PostgresProjectRepository(POSTGRES_DSN)
        self.addCleanup(self.repository.close)
        self.addCleanup(self.projects.close)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute(
                """
                TRUNCATE TABLE
                    artifacts,
                    artifact_batches,
                    tool_audit_events,
                    tool_executions,
                    approval_requests,
                    project_revision_index,
                    project_revisions,
                    projects
                RESTART IDENTITY
                """
            )
        self.alice = UserId("usr_alice")
        self.bob = UserId("usr_bob")
        self.project_id = ProjectId("prj_artifacts")
        self.projects.create_project(
            owner_id=self.alice,
            title="Artifact publication",
            project_id=self.project_id,
            created_at=1.0,
        )
        revision = self.projects.commit_revision(
            owner_id=self.alice,
            project_id=self.project_id,
            document_type=DocumentType.GENERATION_PLAN,
            payload={"operation": "text2music"},
            expected_working_revision_id=None,
            actor="user",
            reason="approved plan",
            revision_id=RevisionId("rev_artifact_plan"),
            created_at=2.0,
        )
        self.specs = (
            ArtifactSpec(
                ArtifactId("art_audio"),
                "artifacts/tenant/project/batch/art_audio",
                "audio/wav",
                5,
                hashlib.sha256(b"audio").hexdigest(),
                (revision.id,),
            ),
            ArtifactSpec(
                ArtifactId("art_manifest"),
                "artifacts/tenant/project/batch/art_manifest",
                "application/json",
                3,
                hashlib.sha256(b"{}\n").hexdigest(),
                (revision.id,),
            ),
        )

    def test_pending_is_invisible_then_atomic_publish_survives_restart(self) -> None:
        pending = self.repository.reserve_batch(
            owner_id=self.alice,
            project_id=self.project_id,
            batch_id="batch_artifacts",
            artifacts=self.specs,
            created_at=3.0,
        )
        self.assertIs(ArtifactPublicationState.PENDING, pending.state)
        with self.assertRaises(KeyError):
            self.repository.get_published_for_owner(
                owner_id=self.alice,
                artifact_id=ArtifactId("art_audio"),
            )

        published = self.repository.publish_batch(
            owner_id=self.alice,
            project_id=self.project_id,
            batch_id="batch_artifacts",
            published_at=4.0,
        )
        self.assertIs(ArtifactPublicationState.PUBLISHED, published.state)
        self.assertTrue(
            all(item.state is ArtifactPublicationState.PUBLISHED for item in published.artifacts)
        )
        reopened = PostgresArtifactRepository(POSTGRES_DSN)
        self.addCleanup(reopened.close)
        recovered = reopened.get_published_for_owner(
            owner_id=self.alice,
            artifact_id=ArtifactId("art_audio"),
        )
        self.assertEqual(self.specs[0].sha256, recovered.sha256)
        with self.assertRaises(KeyError):
            reopened.get_published_for_owner(
                owner_id=self.bob,
                artifact_id=ArtifactId("art_audio"),
            )

    def test_exact_retry_is_idempotent_and_changed_contract_is_rejected(self) -> None:
        first = self.repository.reserve_batch(
            owner_id=self.alice,
            project_id=self.project_id,
            batch_id="batch_artifacts",
            artifacts=self.specs,
        )
        second = self.repository.reserve_batch(
            owner_id=self.alice,
            project_id=self.project_id,
            batch_id="batch_artifacts",
            artifacts=self.specs,
        )
        self.assertEqual(first, second)
        changed = (
            ArtifactSpec(
                self.specs[0].artifact_id,
                self.specs[0].object_key,
                self.specs[0].media_type,
                self.specs[0].size_bytes,
                "0" * 64,
                self.specs[0].source_revision_ids,
            ),
            self.specs[1],
        )
        with self.assertRaises(ArtifactPublicationConflictError):
            self.repository.reserve_batch(
                owner_id=self.alice,
                project_id=self.project_id,
                batch_id="batch_artifacts",
                artifacts=changed,
            )
        with self.assertRaises(KeyError):
            self.repository.reserve_batch(
                owner_id=self.bob,
                project_id=self.project_id,
                batch_id="batch_unauthorized",
                artifacts=self.specs,
            )

    def test_unknown_revision_and_database_mutation_are_rejected(self) -> None:
        unknown_revision = (
            ArtifactSpec(
                ArtifactId("art_unknown"),
                "artifacts/tenant/project/batch/art_unknown",
                "audio/wav",
                1,
                hashlib.sha256(b"x").hexdigest(),
                (RevisionId("rev_unknown"),),
            ),
        )
        with self.assertRaises(KeyError):
            self.repository.reserve_batch(
                owner_id=self.alice,
                project_id=self.project_id,
                batch_id="batch_unknown",
                artifacts=unknown_revision,
            )
        self.repository.reserve_batch(
            owner_id=self.alice,
            project_id=self.project_id,
            batch_id="batch_artifacts",
            artifacts=self.specs,
        )
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            with self.assertRaises(psycopg.Error):
                connection.execute(
                    "UPDATE artifacts SET sha256 = %s WHERE owner_id = %s AND id = %s",
                    ("0" * 64, str(self.alice), "art_audio"),
                )
            with self.assertRaises(psycopg.Error):
                connection.execute(
                    "DELETE FROM artifact_batches WHERE owner_id = %s AND batch_id = %s",
                    (str(self.alice), "batch_artifacts"),
                )


if __name__ == "__main__":
    unittest.main()
