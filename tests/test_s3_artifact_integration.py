"""Real PostgreSQL plus S3-compatible artifact publication contract."""

from __future__ import annotations

import hashlib
import os
import time
import unittest

import boto3
import psycopg
from botocore.config import Config

from agent_music.artifact_factory import (
    build_artifact_reconciliation_service,
    build_artifact_service,
)
from agent_music.artifact_reconciliation import ArtifactReconciliationPolicy
from agent_music.artifact_store import ArtifactWrite
from agent_music.config import HarnessSettings
from agent_music.domain import (
    ArtifactId,
    DocumentType,
    ProjectId,
    RevisionId,
    UserId,
)
from agent_music.object_store import ObjectIntegrityError
from agent_music.postgres_project_repository import PostgresProjectRepository

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")
S3_ENDPOINT = os.getenv("MELQIVRA_TEST_S3_ENDPOINT", "")


@unittest.skipUnless(
    POSTGRES_DSN and S3_ENDPOINT,
    "PostgreSQL and S3-compatible test services are not configured",
)
class S3ArtifactIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = UserId("usr_s3_alice")
        self.project_id = ProjectId("prj_s3_contract")
        projects = PostgresProjectRepository(POSTGRES_DSN)
        self.addCleanup(projects.close)
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
        projects.create_project(
            owner_id=self.owner,
            title="S3 contract",
            project_id=self.project_id,
            created_at=1.0,
        )
        self.revision = projects.commit_revision(
            owner_id=self.owner,
            project_id=self.project_id,
            document_type=DocumentType.GENERATION_PLAN,
            payload={"operation": "text2music"},
            expected_working_revision_id=None,
            actor="user",
            reason="integration contract",
            revision_id=RevisionId("rev_s3_plan"),
            created_at=2.0,
        )
        self.settings = HarnessSettings(
            runner="fake",
            job_store="postgres",
            postgres_dsn=POSTGRES_DSN,
            artifact_storage="s3",
            s3_endpoint_url=S3_ENDPOINT,
            s3_bucket="melqivra-artifacts",
            s3_region="us-east-1",
            s3_create_bucket=True,
            s3_addressing_style="path",
        )
        self.service = build_artifact_service(self.settings)
        self.addCleanup(self.service.close)
        self.writes = (
            ArtifactWrite(
                ArtifactId("art_s3_audio"),
                "audio/wav",
                b"real-s3-audio",
                (self.revision.id,),
            ),
            ArtifactWrite(
                ArtifactId("art_s3_manifest"),
                "application/json",
                b'{"schema":1}\n',
                (self.revision.id,),
            ),
        )

    def test_publish_retry_authorize_and_detect_remote_corruption(self) -> None:
        published = self.service.publish_batch(
            owner_id=self.owner,
            project_id=self.project_id,
            batch_id="batch_s3_contract",
            writes=self.writes,
        )
        self.assertEqual(
            published,
            self.service.publish_batch(
                owner_id=self.owner,
                project_id=self.project_id,
                batch_id="batch_s3_contract",
                writes=self.writes,
            ),
        )
        downloaded = self.service.read_for_owner(
            owner_id=self.owner,
            artifact_id=ArtifactId("art_s3_audio"),
        )
        self.assertEqual(b"real-s3-audio", downloaded.content)
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), downloaded.sha256)
        with self.assertRaises(KeyError):
            self.service.read_for_owner(
                owner_id=UserId("usr_s3_bob"),
                artifact_id=ArtifactId("art_s3_audio"),
            )

        with psycopg.connect(POSTGRES_DSN) as connection:
            row = connection.execute(
                "SELECT object_key FROM artifacts WHERE owner_id = %s AND id = %s",
                (str(self.owner), "art_s3_audio"),
            ).fetchone()
        assert row is not None
        client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        try:
            client.put_object(
                Bucket="melqivra-artifacts",
                Key=str(row[0]),
                Body=b"tampered",
                ContentType="audio/wav",
                Metadata={"sha256": downloaded.sha256},
            )
        finally:
            client.close()
        with self.assertRaises(ObjectIntegrityError):
            self.service.read_for_owner(
                owner_id=self.owner,
                artifact_id=ArtifactId("art_s3_audio"),
            )

        verification_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        try:
            verification_client.put_object(
                Bucket="melqivra-artifacts",
                Key="artifacts/orphan/untracked",
                Body=b"orphan",
                ContentType="application/octet-stream",
            )
            reconciler = build_artifact_reconciliation_service(
                self.settings,
                ArtifactReconciliationPolicy(
                    pending_timeout_seconds=0,
                    corrupt_retention_seconds=0,
                    orphan_grace_seconds=0,
                ),
            )
            try:
                report = reconciler.reconcile(now=time.time() + 1)
            finally:
                reconciler.close()
            self.assertEqual(1, report.deleted_corrupt_batches)
            self.assertEqual(1, report.deleted_orphan_objects)
            remaining = verification_client.list_objects_v2(
                Bucket="melqivra-artifacts",
                Prefix="artifacts/",
            ).get("Contents", [])
            self.assertEqual([], remaining)
        finally:
            verification_client.close()
        with psycopg.connect(POSTGRES_DSN) as connection:
            state = connection.execute(
                """
                SELECT state FROM artifact_batches
                WHERE owner_id = %s AND batch_id = %s
                """,
                (str(self.owner), "batch_s3_contract"),
            ).fetchone()
        self.assertEqual("deleted", state[0] if state is not None else None)


if __name__ == "__main__":
    unittest.main()
