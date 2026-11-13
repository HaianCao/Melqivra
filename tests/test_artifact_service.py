"""Atomic artifact publication service tests without external infrastructure."""

from __future__ import annotations

import hashlib
import time
import unittest
from dataclasses import replace

from agent_music.artifact_repository import (
    ArtifactBatchRecord,
    ArtifactPublicationConflictError,
    ArtifactPublicationState,
    ArtifactRecord,
    ArtifactSpec,
    artifact_batch_contract_sha256,
)
from agent_music.artifact_service import ArtifactPublicationService
from agent_music.artifact_store import ArtifactWrite
from agent_music.domain import ArtifactId, ProjectId, RevisionId, UserId
from agent_music.object_store import (
    ObjectConflictError,
    ObjectInfo,
    ObjectIntegrityError,
    ObjectStoreError,
)


class MemoryArtifactRepository:
    def __init__(self, projects: dict[ProjectId, UserId]) -> None:
        self.projects = projects
        self.batches: dict[tuple[UserId, ProjectId, str], ArtifactBatchRecord] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def reserve_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        artifacts: tuple[ArtifactSpec, ...],
        created_at: float | None = None,
    ) -> ArtifactBatchRecord:
        if self.projects.get(project_id) != owner_id:
            raise KeyError(str(project_id))
        key = (owner_id, project_id, batch_id)
        contract = artifact_batch_contract_sha256(artifacts)
        prior = self.batches.get(key)
        if prior is not None:
            if prior.contract_sha256 != contract:
                raise ArtifactPublicationConflictError("changed artifact batch")
            return prior
        timestamp = time.time() if created_at is None else created_at
        records = tuple(
            ArtifactRecord(
                owner_id=owner_id,
                project_id=project_id,
                batch_id=batch_id,
                ordinal=ordinal,
                artifact_id=spec.artifact_id,
                object_key=spec.object_key,
                media_type=spec.media_type,
                size_bytes=spec.size_bytes,
                sha256=spec.sha256,
                source_revision_ids=spec.source_revision_ids,
                state=ArtifactPublicationState.PENDING,
                created_at=timestamp,
                published_at=None,
            )
            for ordinal, spec in enumerate(artifacts)
        )
        batch = ArtifactBatchRecord(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            contract_sha256=contract,
            state=ArtifactPublicationState.PENDING,
            created_at=timestamp,
            published_at=None,
            artifacts=records,
        )
        self.batches[key] = batch
        return batch

    def publish_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        published_at: float | None = None,
    ) -> ArtifactBatchRecord:
        key = (owner_id, project_id, batch_id)
        prior = self.batches[key]
        timestamp = time.time() if published_at is None else published_at
        artifacts = tuple(
            replace(
                artifact,
                state=ArtifactPublicationState.PUBLISHED,
                published_at=timestamp,
            )
            for artifact in prior.artifacts
        )
        published = replace(
            prior,
            state=ArtifactPublicationState.PUBLISHED,
            published_at=timestamp,
            artifacts=artifacts,
        )
        self.batches[key] = published
        return published

    def get_batch_for_owner(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
    ) -> ArtifactBatchRecord:
        try:
            return self.batches[(owner_id, project_id, batch_id)]
        except KeyError:
            raise KeyError(batch_id) from None

    def get_published_for_owner(
        self,
        *,
        owner_id: UserId,
        artifact_id: ArtifactId,
    ) -> ArtifactRecord:
        for batch in self.batches.values():
            if batch.owner_id != owner_id or batch.state is not ArtifactPublicationState.PUBLISHED:
                continue
            for artifact in batch.artifacts:
                if artifact.artifact_id == artifact_id:
                    return artifact
        raise KeyError(str(artifact_id))


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.fail_after: int | None = None
        self.put_count = 0
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def put_immutable(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        sha256: str,
    ) -> ObjectInfo:
        if self.fail_after is not None and self.put_count >= self.fail_after:
            raise ObjectStoreError("injected object-store interruption")
        self.put_count += 1
        prior = self.objects.get(key)
        value = (content, media_type, sha256)
        if prior is not None and prior != value:
            raise ObjectConflictError("changed immutable object")
        self.objects[key] = value
        return ObjectInfo(key, len(content), sha256, media_type, None)

    def stat(self, key: str) -> ObjectInfo | None:
        value = self.objects.get(key)
        if value is None:
            return None
        content, media_type, sha256 = value
        return ObjectInfo(key, len(content), sha256, media_type, None)

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        try:
            content = self.objects[key][0]
        except KeyError:
            raise KeyError(key) from None
        if max_bytes is not None and len(content) > max_bytes:
            raise ObjectIntegrityError("object exceeds bound")
        return content

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def list(self, prefix: str) -> tuple[ObjectInfo, ...]:
        return tuple(
            item
            for key in sorted(self.objects)
            if key.startswith(prefix)
            for item in (self.stat(key),)
            if item is not None
        )


class ArtifactPublicationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = UserId("usr_alice")
        self.project = ProjectId("prj_song")
        self.repository = MemoryArtifactRepository({self.project: self.owner})
        self.objects = MemoryObjectStore()
        self.service = ArtifactPublicationService(self.repository, self.objects)
        self.writes = (
            ArtifactWrite(
                ArtifactId("art_audio"),
                "audio/wav",
                b"audio-bytes",
                (RevisionId("rev_plan"),),
            ),
            ArtifactWrite(
                ArtifactId("art_manifest"),
                "application/json",
                b"{}\n",
                (RevisionId("rev_plan"),),
            ),
        )

    def test_interrupted_batch_stays_hidden_then_exact_retry_publishes(self) -> None:
        self.objects.fail_after = 1
        with self.assertRaises(ObjectStoreError):
            self.service.publish_batch(
                owner_id=self.owner,
                project_id=self.project,
                batch_id="batch_generation",
                writes=self.writes,
            )
        batch = self.repository.get_batch_for_owner(
            owner_id=self.owner,
            project_id=self.project,
            batch_id="batch_generation",
        )
        self.assertIs(ArtifactPublicationState.PENDING, batch.state)
        with self.assertRaises(KeyError):
            self.repository.get_published_for_owner(
                owner_id=self.owner,
                artifact_id=ArtifactId("art_audio"),
            )

        self.objects.fail_after = None
        published = self.service.publish_batch(
            owner_id=self.owner,
            project_id=self.project,
            batch_id="batch_generation",
            writes=self.writes,
        )
        self.assertEqual(["art_audio", "art_manifest"], [str(item.id) for item in published])
        self.assertTrue(all(item.relative_path.startswith("artifacts/") for item in published))
        self.assertFalse(any(key in str(published) for key in self.objects.objects))

    def test_exact_retry_is_idempotent_and_changed_retry_conflicts(self) -> None:
        first = self.service.publish_batch(
            owner_id=self.owner,
            project_id=self.project,
            batch_id="batch_generation",
            writes=self.writes,
        )
        second = self.service.publish_batch(
            owner_id=self.owner,
            project_id=self.project,
            batch_id="batch_generation",
            writes=self.writes,
        )
        self.assertEqual(first, second)
        changed = (replace(self.writes[0], content=b"changed"), self.writes[1])
        with self.assertRaises(ArtifactPublicationConflictError):
            self.service.publish_batch(
                owner_id=self.owner,
                project_id=self.project,
                batch_id="batch_generation",
                writes=changed,
            )

    def test_authorized_read_verifies_checksum_and_hides_cross_tenant(self) -> None:
        self.service.publish_batch(
            owner_id=self.owner,
            project_id=self.project,
            batch_id="batch_generation",
            writes=self.writes,
        )
        downloaded = self.service.read_for_owner(
            owner_id=self.owner,
            artifact_id=ArtifactId("art_audio"),
        )
        self.assertEqual(b"audio-bytes", downloaded.content)
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), downloaded.sha256)
        with self.assertRaises(KeyError):
            self.service.read_for_owner(
                owner_id=UserId("usr_bob"),
                artifact_id=ArtifactId("art_audio"),
            )

        record = self.repository.get_published_for_owner(
            owner_id=self.owner,
            artifact_id=ArtifactId("art_audio"),
        )
        _content, media_type, sha256 = self.objects.objects[record.object_key]
        self.objects.objects[record.object_key] = (b"tampered", media_type, sha256)
        with self.assertRaises(ObjectIntegrityError):
            self.service.read_for_owner(
                owner_id=self.owner,
                artifact_id=ArtifactId("art_audio"),
            )

    def test_published_retry_classifies_a_missing_object_as_integrity_failure(self) -> None:
        self.service.publish_batch(
            owner_id=self.owner,
            project_id=self.project,
            batch_id="batch_generation",
            writes=self.writes,
        )
        record = self.repository.get_published_for_owner(
            owner_id=self.owner,
            artifact_id=ArtifactId("art_audio"),
        )
        self.objects.delete(record.object_key)
        with self.assertRaises(ObjectIntegrityError):
            self.service.publish_batch(
                owner_id=self.owner,
                project_id=self.project,
                batch_id="batch_generation",
                writes=self.writes,
            )

    def test_close_releases_both_adapters(self) -> None:
        self.service.close()
        self.assertTrue(self.repository.closed)
        self.assertTrue(self.objects.closed)


if __name__ == "__main__":
    unittest.main()
