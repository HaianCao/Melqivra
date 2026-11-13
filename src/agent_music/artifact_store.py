"""Atomic, checksummed storage for immutable project artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_music.domain import (
    Artifact,
    ArtifactId,
    ProjectId,
    RevisionId,
    UserId,
    validate_identifier,
)

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


class CorruptArtifactError(RuntimeError):
    """An artifact no longer matches its recorded content contract."""


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    """One immutable artifact prepared for an atomic batch commit."""

    artifact_id: ArtifactId
    media_type: str
    content: bytes
    source_revision_ids: tuple[RevisionId, ...]


class AtomicArtifactStore:
    """Store immutable bytes below a validated, project-scoped root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        *,
        project_id: ProjectId,
        artifact_id: ArtifactId,
        media_type: str,
        content: bytes,
        source_revision_ids: tuple[RevisionId, ...],
    ) -> Artifact:
        """Atomically commit a new artifact without overwriting an existing one."""

        validate_identifier(str(project_id))
        validate_identifier(str(artifact_id))
        if not _MEDIA_TYPE.fullmatch(media_type.lower()):
            raise ValueError("Invalid media type")
        project_directory = self._project_directory(project_id)
        project_directory.mkdir(parents=True, exist_ok=True)
        destination = project_directory / str(artifact_id)
        if destination.exists():
            raise FileExistsError(str(artifact_id))
        descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=project_directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if destination.exists():
                raise FileExistsError(str(artifact_id))
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        return Artifact(
            id=artifact_id,
            project_id=project_id,
            media_type=media_type.lower(),
            size_bytes=len(content),
            sha256=digest,
            relative_path=destination.relative_to(self._root).as_posix(),
            source_revision_ids=source_revision_ids,
        )

    def put_batch(
        self,
        *,
        project_id: ProjectId,
        batch_id: str,
        writes: tuple[ArtifactWrite, ...],
    ) -> tuple[Artifact, ...]:
        """Atomically commit or verify an exact, idempotent artifact batch."""

        validate_identifier(str(project_id))
        validate_identifier(batch_id)
        if not writes:
            raise ValueError("Artifact batch must not be empty")
        observed_ids: set[ArtifactId] = set()
        for write in writes:
            validate_identifier(str(write.artifact_id))
            if write.artifact_id in observed_ids:
                raise ValueError("Artifact batch IDs must be unique")
            if not _MEDIA_TYPE.fullmatch(write.media_type.lower()):
                raise ValueError("Invalid media type")
            observed_ids.add(write.artifact_id)

        project_directory = self._project_directory(project_id)
        project_directory.mkdir(parents=True, exist_ok=True)
        destination = project_directory / batch_id
        artifacts = tuple(self._batch_artifact(project_id, batch_id, write) for write in writes)
        if destination.exists():
            self._verify_existing_batch(destination, writes)
            return artifacts

        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=project_directory))
        try:
            for write in writes:
                path = staging / str(write.artifact_id)
                with path.open("xb") as stream:
                    stream.write(write.content)
                    stream.flush()
                    os.fsync(stream.fileno())
            if destination.exists():
                raise FileExistsError(batch_id)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return artifacts

    def publish_batch(
        self,
        *,
        owner_id: UserId,
        project_id: ProjectId,
        batch_id: str,
        writes: tuple[ArtifactWrite, ...],
    ) -> tuple[Artifact, ...]:
        """Publish through the shared contract for the filesystem development adapter."""

        _ = owner_id
        return self.put_batch(project_id=project_id, batch_id=batch_id, writes=writes)

    def read(self, *, project_id: ProjectId, artifact: Artifact) -> bytes:
        """Read an authorized artifact and verify its path, length, and checksum."""

        if artifact.project_id != project_id:
            raise KeyError(str(artifact.id))
        project_directory = self._project_directory(project_id)
        recorded = (self._root / artifact.relative_path).resolve()
        try:
            relative = recorded.relative_to(project_directory)
        except ValueError as error:
            raise CorruptArtifactError(str(artifact.id)) from error
        valid_shape = relative.parts == (str(artifact.id),) or (
            len(relative.parts) == 2
            and relative.parts[1] == str(artifact.id)
            and not relative.parts[0].startswith(".pending-")
        )
        if not valid_shape or any(part.startswith(".pending-") for part in relative.parts):
            raise CorruptArtifactError(str(artifact.id))
        try:
            content = recorded.read_bytes()
        except FileNotFoundError as error:
            raise KeyError(str(artifact.id)) from error
        if len(content) != artifact.size_bytes:
            raise CorruptArtifactError(str(artifact.id))
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise CorruptArtifactError(str(artifact.id))
        return content

    def _project_directory(self, project_id: ProjectId) -> Path:
        validate_identifier(str(project_id))
        directory = (self._root / "projects" / str(project_id) / "artifacts").resolve()
        if not directory.is_relative_to(self._root):
            raise ValueError("Artifact path escaped storage root")
        return directory

    def _batch_artifact(
        self,
        project_id: ProjectId,
        batch_id: str,
        write: ArtifactWrite,
    ) -> Artifact:
        relative_path = (
            self._project_directory(project_id) / batch_id / str(write.artifact_id)
        ).relative_to(self._root)
        return Artifact(
            id=write.artifact_id,
            project_id=project_id,
            media_type=write.media_type.lower(),
            size_bytes=len(write.content),
            sha256=hashlib.sha256(write.content).hexdigest(),
            relative_path=relative_path.as_posix(),
            source_revision_ids=write.source_revision_ids,
        )

    @staticmethod
    def _verify_existing_batch(
        destination: Path,
        writes: tuple[ArtifactWrite, ...],
    ) -> None:
        if not destination.is_dir():
            raise CorruptArtifactError(destination.name)
        expected_names = {str(write.artifact_id) for write in writes}
        if {path.name for path in destination.iterdir()} != expected_names:
            raise CorruptArtifactError(destination.name)
        for write in writes:
            content = (destination / str(write.artifact_id)).read_bytes()
            if content != write.content:
                raise CorruptArtifactError(str(write.artifact_id))
