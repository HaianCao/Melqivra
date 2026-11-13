"""Atomic artifact publication across PostgreSQL metadata and immutable bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from agent_music.artifact_repository import (
    ArtifactPublicationConflictError,
    ArtifactPublicationState,
    ArtifactRecord,
    ArtifactRepository,
    ArtifactSpec,
)
from agent_music.artifact_store import ArtifactWrite
from agent_music.domain import Artifact, ArtifactId, ProjectId, UserId
from agent_music.object_store import ObjectIntegrityError, ObjectStore


class ArtifactPublisher(Protocol):
    """Batch publication contract shared by development and production adapters."""

    def publish_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        writes: tuple[ArtifactWrite, ...],
    ) -> tuple[Artifact, ...]: ...


class ArtifactReader(Protocol):
    """Tenant-authorized artifact download contract for the HTTP boundary."""

    def read_for_owner(
        self,
        *,
        owner_id: UserId,
        artifact_id: ArtifactId,
    ) -> ArtifactDownload: ...


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    """Authorized bytes and public metadata returned by the service boundary."""

    artifact_id: ArtifactId
    project_id: ProjectId
    media_type: str
    size_bytes: int
    sha256: str
    content: bytes


class ArtifactPublicationService:
    """Publish only after every immutable object satisfies reserved metadata."""

    def __init__(self, repository: ArtifactRepository, object_store: ObjectStore) -> None:
        self._repository = repository
        self._object_store = object_store

    def close(self) -> None:
        """Release both metadata and object-store resources."""

        try:
            self._object_store.close()
        finally:
            self._repository.close()

    def publish_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        writes: tuple[ArtifactWrite, ...],
    ) -> tuple[Artifact, ...]:
        """Reserve, upload, read-verify, and atomically expose one exact batch."""

        namespace = hashlib.sha256(f"{owner_id}\0{project_id}\0{batch_id}".encode()).hexdigest()[
            :24
        ]
        specs = tuple(
            ArtifactSpec(
                artifact_id=write.artifact_id,
                object_key=(f"artifacts/{namespace}/{project_id}/{batch_id}/{write.artifact_id}"),
                media_type=write.media_type.lower(),
                size_bytes=len(write.content),
                sha256=hashlib.sha256(write.content).hexdigest(),
                source_revision_ids=write.source_revision_ids,
            )
            for write in writes
        )
        batch = self._repository.reserve_batch(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            artifacts=specs,
        )
        if batch.state not in {
            ArtifactPublicationState.PENDING,
            ArtifactPublicationState.PUBLISHED,
        }:
            raise ArtifactPublicationConflictError(
                f"Artifact batch cannot be reused from state {batch.state.value}"
            )

        for write, record in zip(writes, batch.artifacts, strict=True):
            if batch.state is ArtifactPublicationState.PENDING:
                self._object_store.put_immutable(
                    key=record.object_key,
                    content=write.content,
                    media_type=record.media_type,
                    sha256=record.sha256,
                )
            self._read_verified(record)

        if batch.state is ArtifactPublicationState.PENDING:
            batch = self._repository.publish_batch(
                owner_id=owner_id,
                project_id=project_id,
                batch_id=batch_id,
            )
        return tuple(self._public_artifact(record) for record in batch.artifacts)

    def read_for_owner(
        self,
        *,
        owner_id: UserId,
        artifact_id: ArtifactId,
    ) -> ArtifactDownload:
        """Authorize metadata first, then download and verify immutable bytes."""

        record = self._repository.get_published_for_owner(
            owner_id=owner_id,
            artifact_id=artifact_id,
        )
        content = self._read_verified(record)
        return ArtifactDownload(
            artifact_id=record.artifact_id,
            project_id=record.project_id,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            content=content,
        )

    def _read_verified(self, record: ArtifactRecord) -> bytes:
        try:
            content = self._object_store.get(
                record.object_key,
                max_bytes=record.size_bytes,
            )
        except KeyError as error:
            raise ObjectIntegrityError("Artifact object is missing") from error
        self._verify_bytes(record, content)
        return content

    @staticmethod
    def _verify_bytes(record: ArtifactRecord, content: bytes) -> None:
        if len(content) != record.size_bytes:
            raise ObjectIntegrityError("Artifact object length does not match metadata")
        if hashlib.sha256(content).hexdigest() != record.sha256:
            raise ObjectIntegrityError("Artifact object checksum does not match metadata")

    @staticmethod
    def _public_artifact(record: ArtifactRecord) -> Artifact:
        return Artifact(
            id=record.artifact_id,
            project_id=record.project_id,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            relative_path=f"artifacts/{record.artifact_id}",
            source_revision_ids=record.source_revision_ids,
        )
