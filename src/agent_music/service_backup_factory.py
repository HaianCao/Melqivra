"""Explicit production composition for PostgreSQL/S3 service archives."""

from __future__ import annotations

from agent_music.artifact_factory import build_s3_object_store
from agent_music.config import HarnessSettings
from agent_music.service_backup import (
    DockerPostgresArchiveTool,
    LocalPostgresArchiveTool,
    PostgresArchiveTool,
    PostgresServiceBackupCatalog,
    ServiceBackupManager,
)


def build_service_backup_manager(settings: HarnessSettings) -> ServiceBackupManager:
    """Build the configured PostgreSQL and S3 backup/restore operation."""

    settings.validate()
    if settings.job_store != "postgres" or settings.artifact_storage != "s3":
        raise ValueError("Service backup requires PostgreSQL and S3 artifact storage")
    object_store = build_s3_object_store(settings)
    try:
        catalog = PostgresServiceBackupCatalog(
            settings.postgres_dsn,
            connect_timeout_seconds=settings.postgres_connect_timeout_seconds,
        )
        archive_tool: PostgresArchiveTool
        if settings.service_archive_mode == "docker":
            archive_tool = DockerPostgresArchiveTool(
                docker_executable=settings.docker_executable,
                image=settings.postgres_archive_docker_image,
                timeout_seconds=settings.service_archive_timeout_seconds,
            )
        else:
            archive_tool = LocalPostgresArchiveTool(
                dump_executable=settings.postgres_dump_executable,
                restore_executable=settings.postgres_restore_executable,
                timeout_seconds=settings.service_archive_timeout_seconds,
            )
        return ServiceBackupManager(
            postgres_dsn=settings.postgres_dsn,
            catalog=catalog,
            object_store=object_store,
            archive_tool=archive_tool,
            max_database_bytes=settings.service_archive_max_database_bytes,
            max_objects=settings.service_archive_max_objects,
            max_total_object_bytes=settings.service_archive_max_total_object_bytes,
            object_page_size=settings.service_archive_object_page_size,
        )
    except BaseException:
        object_store.close()
        raise
