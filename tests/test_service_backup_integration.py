"""Real PostgreSQL plus S3-compatible disaster backup/restore contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_music.approval_repository import ApprovalPolicyError, ApprovalRequest, RiskClass
from agent_music.artifact_factory import build_artifact_service
from agent_music.artifact_store import ArtifactWrite
from agent_music.config import HarnessSettings
from agent_music.domain import ArtifactId, DocumentType, ProjectId, RevisionId, UserId
from agent_music.job_store import JobStore
from agent_music.models import JobState
from agent_music.postgres_approval_repository import PostgresApprovalRepository
from agent_music.postgres_job_store import PostgresJobStore
from agent_music.postgres_migrations import POSTGRES_MIGRATIONS
from agent_music.postgres_project_repository import PostgresProjectRepository
from agent_music.postgres_tool_execution_repository import PostgresToolExecutionRepository
from agent_music.service_backup_factory import build_service_backup_manager
from agent_music.tool_execution_repository import (
    AuditEvent,
    ToolExecutionDisposition,
    tool_input_sha256,
)

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")
S3_ENDPOINT = os.getenv("MELQIVRA_TEST_S3_ENDPOINT", "")
POSTGRES_CLIENT_IMAGE = (
    "postgres:17-alpine@sha256:d3244e8ce3f165ee3c93d9c4a4b1d0fe48b3a89e2287b717fdcbbde92d405065"
)


@unittest.skipUnless(
    POSTGRES_DSN and S3_ENDPOINT,
    "PostgreSQL and S3-compatible test services are not configured",
)
class ServiceBackupIntegrationTests(unittest.TestCase):
    def test_fresh_restore_preserves_authority_history_isolation_and_artifacts(self) -> None:
        database_name = f"melqivra_restore_{uuid.uuid4().hex[:12]}"
        target_dsn, admin_dsn = _target_connections(POSTGRES_DSN, database_name)
        source_bucket = f"melqivra-backup-source-{uuid.uuid4().hex[:12]}"
        target_bucket = f"melqivra-backup-target-{uuid.uuid4().hex[:12]}"
        _create_database(admin_dsn, database_name)
        try:
            _reset_source_database(POSTGRES_DSN)
            source_settings = _settings(POSTGRES_DSN, source_bucket)
            target_settings = _settings(target_dsn, target_bucket)
            owner = UserId("usr_backup_alice")
            other = UserId("usr_backup_bob")
            project_id = ProjectId("prj_backup_contract")
            revision_id = self._seed_source_authority(
                source_settings,
                owner=owner,
                project_id=project_id,
            )

            with tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "service-backup"
                source_manager = build_service_backup_manager(source_settings)
                try:
                    backup = source_manager.create_backup(archive)
                finally:
                    source_manager.close()
                target_manager = build_service_backup_manager(target_settings)
                try:
                    restore = target_manager.restore_backup(archive)
                finally:
                    target_manager.close()

            self.assertEqual(backup.backup_id, restore.backup_id)
            self.assertEqual(len(POSTGRES_MIGRATIONS), restore.database_schema_version)
            self.assertEqual(2, restore.object_count)
            self._verify_restored_authority(
                target_settings,
                owner=owner,
                other=other,
                project_id=project_id,
                revision_id=revision_id,
            )
        finally:
            _delete_bucket(source_bucket)
            _delete_bucket(target_bucket)
            _drop_database(admin_dsn, database_name)

    def _seed_source_authority(
        self,
        settings: HarnessSettings,
        *,
        owner: UserId,
        project_id: ProjectId,
    ) -> RevisionId:
        projects = PostgresProjectRepository(settings.postgres_dsn)
        approvals = PostgresApprovalRepository(settings.postgres_dsn)
        executions = PostgresToolExecutionRepository(settings.postgres_dsn)
        jobs: JobStore = PostgresJobStore(settings.postgres_dsn)
        try:
            projects.create_project(
                owner_id=owner,
                title="Backup contract",
                project_id=project_id,
                created_at=1_000.0,
            )
            revision = projects.commit_revision(
                owner_id=owner,
                project_id=project_id,
                document_type=DocumentType.GENERATION_PLAN,
                payload={"operation": "text2music", "seed": 17},
                expected_working_revision_id=None,
                actor="user",
                reason="backup contract",
                revision_id=RevisionId("rev_backup_plan"),
                created_at=1_001.0,
            )
            request = approvals.propose(
                ApprovalRequest(
                    "approval_backup_gpu",
                    "music.generate:backup-contract",
                    RiskClass.GPU,
                    project_id,
                    owner,
                )
            )
            approvals.decide(
                approval_id=request.approval_id,
                user_id=owner,
                approved=True,
                actor="owner",
            )
            fingerprint = tool_input_sha256("project.read", {})
            claim = executions.begin(
                user_id=owner,
                project_id=project_id,
                tool_call_id="call_backup_replay",
                tool_name="project.read",
                input_sha256=fingerprint,
                actor="agent",
                turn_id="turn_backup",
            )
            self.assertEqual(ToolExecutionDisposition.EXECUTE, claim.disposition)
            executions.complete_success(
                user_id=owner,
                project_id=project_id,
                tool_call_id="call_backup_replay",
                tool_name="project.read",
                input_sha256=fingerprint,
                result={"project_id": str(project_id)},
                event=AuditEvent(
                    owner,
                    project_id,
                    "agent",
                    "project.read",
                    "call_backup_replay",
                    "success",
                    str(revision.id),
                    "turn_backup",
                ),
            )
            job = jobs.enqueue(
                user_id=str(owner),
                kind="music.generate",
                payload={"project_id": str(project_id), "seed": 17},
                idempotency_key="backup-job-request",
            )
            claimed = jobs.claim_next(lease_owner="backup-worker", lease_seconds=60)
            assert claimed is not None and claimed.lease_token is not None
            jobs.append_event(
                job_id=claimed.id,
                lease_token=claimed.lease_token,
                event_type="provider.progress",
                payload={"phase": "generated", "percent": 50},
            )
            jobs.complete(claimed.id, claimed.lease_token, {"artifact_batch": "batch_backup"})
            self.assertEqual(JobState.SUCCEEDED, jobs.get(job.id).state)
        finally:
            jobs.close()
            executions.close()
            approvals.close()
            projects.close()

        artifacts = build_artifact_service(settings)
        try:
            artifacts.publish_batch(
                owner_id=owner,
                project_id=project_id,
                batch_id="batch_backup",
                writes=(
                    ArtifactWrite(
                        ArtifactId("art_backup_audio"),
                        "audio/wav",
                        b"backup-audio-bytes",
                        (revision.id,),
                    ),
                    ArtifactWrite(
                        ArtifactId("art_backup_manifest"),
                        "application/json",
                        b'{"schema_version":1}\n',
                        (revision.id,),
                    ),
                ),
            )
        finally:
            artifacts.close()
        return revision.id

    def _verify_restored_authority(
        self,
        settings: HarnessSettings,
        *,
        owner: UserId,
        other: UserId,
        project_id: ProjectId,
        revision_id: RevisionId,
    ) -> None:
        projects = PostgresProjectRepository(settings.postgres_dsn, migrate=False)
        approvals = PostgresApprovalRepository(settings.postgres_dsn, migrate=False)
        executions = PostgresToolExecutionRepository(settings.postgres_dsn, migrate=False)
        jobs = PostgresJobStore(settings.postgres_dsn, migrate=False)
        try:
            self.assertEqual(
                "Backup contract",
                projects.get_project(owner_id=owner, project_id=project_id).title,
            )
            self.assertEqual(
                17,
                json.loads(
                    projects.get_revision(
                        owner_id=owner,
                        project_id=project_id,
                        revision_id=revision_id,
                    ).payload_json
                )["seed"],
            )
            with self.assertRaises(KeyError):
                projects.get_project(owner_id=other, project_id=project_id)
            approvals.require_approved(
                approval_id="approval_backup_gpu",
                user_id=owner,
                project_id=project_id,
                action="music.generate:backup-contract",
                risk=RiskClass.GPU,
            )
            with self.assertRaises(ApprovalPolicyError):
                approvals.require_approved(
                    approval_id="approval_backup_gpu",
                    user_id=other,
                    project_id=project_id,
                    action="music.generate:backup-contract",
                    risk=RiskClass.GPU,
                )
            fingerprint = tool_input_sha256("project.read", {})
            replay = executions.begin(
                user_id=owner,
                project_id=project_id,
                tool_call_id="call_backup_replay",
                tool_name="project.read",
                input_sha256=fingerprint,
                actor="agent",
                turn_id="turn_backup",
            )
            self.assertEqual(ToolExecutionDisposition.REPLAY, replay.disposition)
            self.assertEqual({"project_id": str(project_id)}, replay.result)
            self.assertEqual(1, len(executions.list_audit_events(user_id=owner)))
            restored_job = jobs.enqueue(
                user_id=str(owner),
                kind="music.generate",
                payload={"project_id": str(project_id), "seed": 999},
                idempotency_key="backup-job-request",
            )
            self.assertEqual(JobState.SUCCEEDED, restored_job.state)
            self.assertEqual(1, len(jobs.list_attempts(restored_job.id)))
            self.assertGreaterEqual(
                len(
                    jobs.list_events_for_user(
                        user_id=str(owner),
                        job_id=restored_job.id,
                        after_sequence=0,
                        limit=100,
                    )
                ),
                4,
            )
            with self.assertRaises(KeyError):
                jobs.get_for_user(user_id=str(other), job_id=restored_job.id)
        finally:
            jobs.close()
            executions.close()
            approvals.close()
            projects.close()

        artifacts = build_artifact_service(settings)
        try:
            audio = artifacts.read_for_owner(
                owner_id=owner,
                artifact_id=ArtifactId("art_backup_audio"),
            )
            self.assertEqual(b"backup-audio-bytes", audio.content)
            with self.assertRaises(KeyError):
                artifacts.read_for_owner(
                    owner_id=other,
                    artifact_id=ArtifactId("art_backup_audio"),
                )
        finally:
            artifacts.close()


def _settings(dsn: str, bucket: str) -> HarnessSettings:
    return HarnessSettings(
        runner="fake",
        job_store="postgres",
        postgres_dsn=dsn,
        artifact_storage="s3",
        s3_endpoint_url=S3_ENDPOINT,
        s3_bucket=bucket,
        s3_region="us-east-1",
        s3_create_bucket=True,
        s3_addressing_style="path",
        service_archive_mode="docker",
        postgres_archive_docker_image=POSTGRES_CLIENT_IMAGE,
    )


def _target_connections(source_dsn: str, database_name: str) -> tuple[str, str]:
    values = conninfo_to_dict(source_dsn)
    values["dbname"] = database_name
    target = make_conninfo(**values)
    values["dbname"] = "postgres"
    return target, make_conninfo(**values)


def _create_database(admin_dsn: str, database_name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))


def _drop_database(admin_dsn: str, database_name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (database_name,),
        )
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
        )


def _reset_source_database(dsn: str) -> None:
    store = PostgresJobStore(dsn)
    store.close()
    with psycopg.connect(dsn, autocommit=True) as connection:
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
                projects,
                gpu_job_events,
                gpu_job_attempts,
                gpu_jobs,
                gpu_capacity_slots
            RESTART IDENTITY
            """
        )
        connection.execute("INSERT INTO gpu_capacity_slots(slot_id) VALUES (1)")
        connection.execute("ALTER SEQUENCE gpu_fencing_token_seq RESTART WITH 1")


def _delete_bucket(bucket: str) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        try:
            response = client.list_objects_v2(Bucket=bucket)
        except Exception:
            return
        for item in response.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=str(item["Key"]))
        client.delete_bucket(Bucket=bucket)
    finally:
        client.close()


if __name__ == "__main__":
    unittest.main()
