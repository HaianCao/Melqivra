"""Atomic, checksummed storage for immutable project artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from agent_music.domain import Artifact, ArtifactId, ProjectId, RevisionId, validate_identifier

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


class CorruptArtifactError(RuntimeError):
    """An artifact no longer matches its recorded content contract."""


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

    def read(self, *, project_id: ProjectId, artifact: Artifact) -> bytes:
        """Read an authorized artifact and verify its path, length, and checksum."""

        if artifact.project_id != project_id:
            raise KeyError(str(artifact.id))
        expected = self._project_directory(project_id) / str(artifact.id)
        recorded = (self._root / artifact.relative_path).resolve()
        if recorded != expected or not recorded.is_relative_to(self._root):
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
