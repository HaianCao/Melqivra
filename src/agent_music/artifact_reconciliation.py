"""Crash-safe artifact retention, deletion, and orphan reconciliation."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from agent_music.artifact_integrity import read_verified_artifact
from agent_music.artifact_repository import (
    ArtifactBatchRecord,
    ArtifactPublicationState,
    ArtifactRepository,
)
from agent_music.domain import ProjectId, UserId
from agent_music.object_store import ObjectIntegrityError, ObjectStore


@dataclass(frozen=True, slots=True)
class ArtifactReconciliationPolicy:
    """Bounded lifecycle policy for one reconciliation pass."""

    pending_timeout_seconds: float = 3_600.0
    corrupt_retention_seconds: float = 86_400.0
    orphan_grace_seconds: float = 86_400.0
    published_retention_seconds: float | None = None
    batch_limit: int = 100
    object_page_size: int = 100

    def validate(self) -> None:
        """Reject unsafe negative, non-finite, or unbounded settings."""

        durations = (
            self.pending_timeout_seconds,
            self.corrupt_retention_seconds,
            self.orphan_grace_seconds,
        )
        if any(not math.isfinite(value) or value < 0 for value in durations):
            raise ValueError("Artifact reconciliation durations must be finite and non-negative")
        if self.published_retention_seconds is not None and (
            not math.isfinite(self.published_retention_seconds)
            or self.published_retention_seconds < 0
        ):
            raise ValueError("Published artifact retention must be finite and non-negative")
        if self.batch_limit < 1 or self.batch_limit > 1_000:
            raise ValueError("Artifact reconciliation batch limit must be between 1 and 1000")
        if self.object_page_size < 1 or self.object_page_size > 1_000:
            raise ValueError("Artifact object page size must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class ArtifactReconciliationReport:
    """Content-free lifecycle counts safe for operational logs and metrics."""

    recovered_pending_batches: int
    deleted_pending_batches: int
    deleted_corrupt_batches: int
    resumed_deleting_batches: int
    expired_published_batches: int
    deleted_orphan_objects: int
    deferred_orphan_objects: int
    next_orphan_cursor: str | None


class ArtifactReconciliationService:
    """Converge metadata and immutable bytes after interruption or retention."""

    def __init__(
        self,
        repository: ArtifactRepository,
        object_store: ObjectStore,
        policy: ArtifactReconciliationPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._object_store = object_store
        self._policy = policy or ArtifactReconciliationPolicy()
        self._policy.validate()

    def close(self) -> None:
        """Release both lifecycle adapters."""

        try:
            self._object_store.close()
        finally:
            self._repository.close()

    def delete_batch_for_owner(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        now: float | None = None,
    ) -> ArtifactBatchRecord:
        """Delete one owned batch through a restart-safe tombstone transition."""

        timestamp = self._timestamp(now)
        batch = self._repository.start_batch_deletion(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            changed_at=timestamp,
        )
        if batch.state is ArtifactPublicationState.DELETED:
            return batch
        for artifact in batch.artifacts:
            self._object_store.delete(artifact.object_key)
        return self._repository.finish_batch_deletion(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
            deleted_at=timestamp,
        )

    def reconcile(
        self,
        *,
        now: float | None = None,
        orphan_cursor: str | None = None,
    ) -> ArtifactReconciliationReport:
        """Run one bounded, resumable convergence pass."""

        timestamp = self._timestamp(now)
        resumed_deleting = 0
        for batch in self._batches(
            ArtifactPublicationState.DELETING,
            changed_before=timestamp,
        ):
            self.delete_batch_for_owner(
                owner_id=batch.owner_id,
                project_id=batch.project_id,
                batch_id=batch.batch_id,
                now=timestamp,
            )
            resumed_deleting += 1

        recovered_pending = 0
        deleted_pending = 0
        for batch in self._batches(
            ArtifactPublicationState.PENDING,
            changed_before=timestamp - self._policy.pending_timeout_seconds,
        ):
            try:
                for artifact in batch.artifacts:
                    read_verified_artifact(self._object_store, artifact)
            except (KeyError, ObjectIntegrityError):
                self.delete_batch_for_owner(
                    owner_id=batch.owner_id,
                    project_id=batch.project_id,
                    batch_id=batch.batch_id,
                    now=timestamp,
                )
                deleted_pending += 1
            else:
                self._repository.publish_batch(
                    owner_id=batch.owner_id,
                    project_id=batch.project_id,
                    batch_id=batch.batch_id,
                    published_at=timestamp,
                )
                recovered_pending += 1

        deleted_corrupt = 0
        for batch in self._batches(
            ArtifactPublicationState.CORRUPT,
            changed_before=timestamp - self._policy.corrupt_retention_seconds,
        ):
            self.delete_batch_for_owner(
                owner_id=batch.owner_id,
                project_id=batch.project_id,
                batch_id=batch.batch_id,
                now=timestamp,
            )
            deleted_corrupt += 1

        expired_published = 0
        retention = self._policy.published_retention_seconds
        if retention is not None:
            for batch in self._batches(
                ArtifactPublicationState.PUBLISHED,
                changed_before=timestamp - retention,
            ):
                self.delete_batch_for_owner(
                    owner_id=batch.owner_id,
                    project_id=batch.project_id,
                    batch_id=batch.batch_id,
                    now=timestamp,
                )
                expired_published += 1

        page = self._object_store.list(
            "artifacts/",
            cursor=orphan_cursor,
            limit=self._policy.object_page_size,
        )
        keys = tuple(item.key for item in page.items)
        known = self._repository.known_live_object_keys(keys)
        deleted_orphans = 0
        deferred_orphans = 0
        orphan_cutoff = timestamp - self._policy.orphan_grace_seconds
        for item in page.items:
            if item.key in known:
                continue
            if item.last_modified_at is None or item.last_modified_at > orphan_cutoff:
                deferred_orphans += 1
                continue
            self._object_store.delete(item.key)
            deleted_orphans += 1

        return ArtifactReconciliationReport(
            recovered_pending_batches=recovered_pending,
            deleted_pending_batches=deleted_pending,
            deleted_corrupt_batches=deleted_corrupt,
            resumed_deleting_batches=resumed_deleting,
            expired_published_batches=expired_published,
            deleted_orphan_objects=deleted_orphans,
            deferred_orphan_objects=deferred_orphans,
            next_orphan_cursor=page.next_cursor,
        )

    def _batches(
        self,
        state: ArtifactPublicationState,
        *,
        changed_before: float,
    ) -> tuple[ArtifactBatchRecord, ...]:
        return self._repository.list_batches_for_reconciliation(
            states=(state,),
            changed_before=changed_before,
            limit=self._policy.batch_limit,
        )

    @staticmethod
    def _timestamp(value: float | None) -> float:
        timestamp = time.time() if value is None else value
        if not math.isfinite(timestamp):
            raise ValueError("Artifact reconciliation time must be finite")
        return timestamp
