"""Shared verification for immutable artifact bytes."""

from __future__ import annotations

import hashlib

from agent_music.artifact_repository import ArtifactRecord
from agent_music.object_store import ObjectIntegrityError, ObjectStore


def read_verified_artifact(object_store: ObjectStore, record: ArtifactRecord) -> bytes:
    """Read within the metadata bound and verify exact length and SHA-256."""

    try:
        content = object_store.get(record.object_key, max_bytes=record.size_bytes)
    except KeyError as error:
        raise ObjectIntegrityError("Artifact object is missing") from error
    if len(content) != record.size_bytes:
        raise ObjectIntegrityError("Artifact object length does not match metadata")
    if hashlib.sha256(content).hexdigest() != record.sha256:
        raise ObjectIntegrityError("Artifact object checksum does not match metadata")
    return content
