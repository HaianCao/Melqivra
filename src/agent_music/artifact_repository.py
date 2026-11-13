"""Durable authority contracts for tenant-scoped artifact publication."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agent_music.domain import ArtifactId, ProjectId, RevisionId, UserId, validate_identifier

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_OBJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9._=-]*(?:/[a-z0-9][a-z0-9._=-]*)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactPublicationConflictError(RuntimeError):
    """An idempotency key was reused for a different artifact contract."""


class CorruptArtifactMetadataError(RuntimeError):
    """Durable artifact metadata no longer satisfies its recorded contract."""


class ArtifactPublicationState(StrEnum):
    """Durable visibility and retention state for an artifact batch."""

    PENDING = "pending"
    PUBLISHED = "published"
    CORRUPT = "corrupt"
    DELETING = "deleting"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """Immutable metadata reserved before object bytes are uploaded."""

    artifact_id: ArtifactId
    object_key: str
    media_type: str
    size_bytes: int
    sha256: str
    source_revision_ids: tuple[RevisionId, ...]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One tenant-owned artifact metadata record."""

    owner_id: UserId
    project_id: ProjectId
    batch_id: str
    ordinal: int
    artifact_id: ArtifactId
    object_key: str
    media_type: str
    size_bytes: int
    sha256: str
    source_revision_ids: tuple[RevisionId, ...]
    state: ArtifactPublicationState
    created_at: float
    state_changed_at: float
    published_at: float | None
    deleted_at: float | None


@dataclass(frozen=True, slots=True)
class ArtifactBatchRecord:
    """Atomic publication state and ordered members for one artifact batch."""

    owner_id: UserId
    project_id: ProjectId
    batch_id: str
    contract_sha256: str
    state: ArtifactPublicationState
    created_at: float
    state_changed_at: float
    published_at: float | None
    deleted_at: float | None
    artifacts: tuple[ArtifactRecord, ...]


class ArtifactRepository(Protocol):
    """PostgreSQL-backed artifact metadata authority used by publication services."""

    def close(self) -> None: ...

    def reserve_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        artifacts: tuple[ArtifactSpec, ...],
        created_at: float | None = None,
    ) -> ArtifactBatchRecord: ...

    def publish_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        published_at: float | None = None,
    ) -> ArtifactBatchRecord: ...

    def get_batch_for_owner(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
    ) -> ArtifactBatchRecord: ...

    def get_published_for_owner(
        self,
        *,
        owner_id: UserId,
        artifact_id: ArtifactId,
    ) -> ArtifactRecord: ...

    def mark_batch_corrupt(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        changed_at: float | None = None,
    ) -> ArtifactBatchRecord: ...

    def start_batch_deletion(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        changed_at: float | None = None,
    ) -> ArtifactBatchRecord: ...

    def finish_batch_deletion(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        deleted_at: float | None = None,
    ) -> ArtifactBatchRecord: ...

    def list_batches_for_reconciliation(
        self,
        *,
        states: tuple[ArtifactPublicationState, ...],
        changed_before: float,
        limit: int,
    ) -> tuple[ArtifactBatchRecord, ...]: ...

    def known_live_object_keys(self, keys: tuple[str, ...]) -> frozenset[str]: ...


def validate_artifact_specs(artifacts: tuple[ArtifactSpec, ...]) -> None:
    """Reject ambiguous or malformed immutable artifact metadata."""

    if not artifacts:
        raise ValueError("Artifact batch must not be empty")
    observed_ids: set[ArtifactId] = set()
    observed_keys: set[str] = set()
    for artifact in artifacts:
        validate_identifier(str(artifact.artifact_id))
        if artifact.artifact_id in observed_ids or artifact.object_key in observed_keys:
            raise ValueError("Artifact IDs and object keys must be unique within a batch")
        if not _MEDIA_TYPE.fullmatch(artifact.media_type):
            raise ValueError("Artifact media type is invalid")
        validate_artifact_object_key(artifact.object_key)
        if artifact.size_bytes < 0:
            raise ValueError("Artifact size must not be negative")
        if _SHA256.fullmatch(artifact.sha256) is None:
            raise ValueError("Artifact SHA-256 is invalid")
        if len(set(artifact.source_revision_ids)) != len(artifact.source_revision_ids):
            raise ValueError("Artifact source revisions must be unique")
        for revision_id in artifact.source_revision_ids:
            validate_identifier(str(revision_id))
        observed_ids.add(artifact.artifact_id)
        observed_keys.add(artifact.object_key)


def validate_artifact_object_key(key: str) -> None:
    """Reject object keys outside the service-owned portable key grammar."""

    if len(key.encode("utf-8")) > 1_024 or _OBJECT_KEY.fullmatch(key) is None:
        raise ValueError("Artifact object key is invalid")


def artifact_batch_contract_sha256(artifacts: tuple[ArtifactSpec, ...]) -> str:
    """Return the canonical exact-retry contract for an ordered batch."""

    validate_artifact_specs(artifacts)
    payload = [
        {
            "artifact_id": str(artifact.artifact_id),
            "media_type": artifact.media_type,
            "object_key": artifact.object_key,
            "ordinal": ordinal,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "source_revision_ids": [str(item) for item in artifact.source_revision_ids],
        }
        for ordinal, artifact in enumerate(artifacts)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
