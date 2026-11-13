"""Explicit production composition for PostgreSQL and S3 artifact adapters."""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast

from agent_music.artifact_reconciliation import (
    ArtifactReconciliationPolicy,
    ArtifactReconciliationService,
)
from agent_music.artifact_repository import ArtifactRepository
from agent_music.artifact_service import ArtifactPublicationService
from agent_music.config import HarnessSettings
from agent_music.object_store import S3Client, S3ObjectStore
from agent_music.postgres_artifact_repository import PostgresArtifactRepository


def build_artifact_service(settings: HarnessSettings) -> ArtifactPublicationService:
    """Build the configured production artifact publication service."""

    repository = _build_artifact_repository(settings)
    try:
        object_store = build_s3_object_store(settings)
    except BaseException:
        repository.close()
        raise
    return ArtifactPublicationService(repository, object_store)


def build_artifact_reconciliation_service(
    settings: HarnessSettings,
    policy: ArtifactReconciliationPolicy | None = None,
) -> ArtifactReconciliationService:
    """Build an independently owned production artifact lifecycle worker."""

    repository = _build_artifact_repository(settings)
    try:
        object_store = build_s3_object_store(settings)
    except BaseException:
        repository.close()
        raise
    resolved_policy = policy or ArtifactReconciliationPolicy(
        pending_timeout_seconds=settings.artifact_pending_timeout_seconds,
        corrupt_retention_seconds=settings.artifact_corrupt_retention_seconds,
        orphan_grace_seconds=settings.artifact_orphan_grace_seconds,
        published_retention_seconds=settings.artifact_published_retention_seconds,
        batch_limit=settings.artifact_reconcile_batch_limit,
        object_page_size=settings.artifact_reconcile_object_page_size,
    )
    return ArtifactReconciliationService(repository, object_store, resolved_policy)


def _build_artifact_repository(settings: HarnessSettings) -> ArtifactRepository:
    settings.validate()
    if settings.artifact_storage != "s3":
        raise ValueError("Production artifact service requires ARTIFACT_STORAGE=s3")
    return PostgresArtifactRepository(
        settings.postgres_dsn,
        min_pool_size=settings.postgres_pool_min_size,
        max_pool_size=settings.postgres_pool_max_size,
        connection_timeout_seconds=settings.postgres_connect_timeout_seconds,
    )


def build_s3_object_store(settings: HarnessSettings) -> S3ObjectStore:
    """Build the configured S3-compatible immutable object adapter."""

    settings.validate()
    if settings.artifact_storage != "s3":
        raise ValueError("S3 object store requires ARTIFACT_STORAGE=s3")
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
    return S3ObjectStore(
        client,
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        create_bucket=settings.s3_create_bucket,
    )
