"""Explicit production composition for PostgreSQL and S3 artifact adapters."""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast

from agent_music.artifact_service import ArtifactPublicationService
from agent_music.config import HarnessSettings
from agent_music.object_store import S3Client, S3ObjectStore
from agent_music.postgres_artifact_repository import PostgresArtifactRepository


def build_artifact_service(settings: HarnessSettings) -> ArtifactPublicationService:
    """Build the configured production artifact publication service."""

    settings.validate()
    if settings.artifact_storage != "s3":
        raise ValueError("Production artifact service requires ARTIFACT_STORAGE=s3")
    repository = PostgresArtifactRepository(
        settings.postgres_dsn,
        min_pool_size=settings.postgres_pool_min_size,
        max_pool_size=settings.postgres_pool_max_size,
        connection_timeout_seconds=settings.postgres_connect_timeout_seconds,
    )
    try:
        boto3 = import_module("boto3")
        config_type = import_module("botocore.config").Config
        config = config_type(
            signature_version="s3v4",
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            retries={"max_attempts": settings.s3_max_attempts, "mode": "standard"},
            s3={"addressing_style": settings.s3_addressing_style},
        )
        arguments: dict[str, Any] = {
            "region_name": settings.s3_region,
            "config": config,
        }
        if settings.s3_endpoint_url:
            arguments["endpoint_url"] = settings.s3_endpoint_url
        client = cast(S3Client, boto3.client("s3", **arguments))
        object_store = S3ObjectStore(
            client,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            create_bucket=settings.s3_create_bucket,
        )
    except BaseException:
        repository.close()
        raise
    return ArtifactPublicationService(repository, object_store)
