"""Deterministic project archive export and import."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agent_music.domain import DocumentType, JsonObject, JsonValue, ProjectId, RevisionId, UserId
from agent_music.project_store import SqliteProjectRepository, canonical_json, content_checksum

ARCHIVE_VERSION = 1
MANIFEST_NAME = "project.json"


class InvalidProjectArchiveError(ValueError):
    """An imported archive violates the portable project contract."""


def export_project_archive(
    repository: SqliteProjectRepository,
    *,
    owner_id: UserId,
    project_id: ProjectId,
    destination: Path,
) -> None:
    """Export project metadata and immutable history to a deterministic ZIP."""

    project = repository.get_project(owner_id=owner_id, project_id=project_id)
    index = repository.get_index(owner_id=owner_id, project_id=project_id)
    revisions: list[JsonObject] = []
    for document_type in DocumentType:
        for revision in repository.list_revisions(
            owner_id=owner_id, project_id=project_id, document_type=document_type
        ):
            revisions.append(
                {
                    "id": str(revision.id),
                    "document_type": revision.document_type.value,
                    "parent_revision_id": (
                        str(revision.parent_revision_id) if revision.parent_revision_id else None
                    ),
                    "payload_json": revision.payload_json,
                    "checksum": revision.checksum,
                    "actor": revision.actor,
                    "reason": revision.reason,
                    "created_at": revision.created_at,
                }
            )
    selections: list[JsonObject] = [
        {
            "document_type": selection.document_type.value,
            "working_revision_id": (
                str(selection.working_revision_id) if selection.working_revision_id else None
            ),
            "accepted_revision_id": (
                str(selection.accepted_revision_id) if selection.accepted_revision_id else None
            ),
        }
        for selection in index.selections
    ]
    manifest: JsonObject = {
        "archive_version": ARCHIVE_VERSION,
        "project": {
            "id": str(project.id),
            "title": project.title,
            "created_at": project.created_at,
        },
        "revisions": cast(list[JsonValue], revisions),
        "selections": cast(list[JsonValue], selections),
    }
    payload = canonical_json(manifest).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o600 << 16
        archive.writestr(info, payload)


def import_project_archive(
    repository: SqliteProjectRepository,
    *,
    owner_id: UserId,
    source: Path,
) -> ProjectId:
    """Import a validated archive, preserving identifiers, history, and checksums."""

    try:
        with zipfile.ZipFile(source, "r") as archive:
            names = archive.namelist()
            if names != [MANIFEST_NAME]:
                raise InvalidProjectArchiveError("Archive contains unexpected entries")
            if archive.getinfo(MANIFEST_NAME).file_size > 8 * 1024 * 1024:
                raise InvalidProjectArchiveError("Archive manifest is too large")
            raw = archive.read(MANIFEST_NAME)
        value = json.loads(raw)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise InvalidProjectArchiveError("Unreadable project archive") from error
    if not isinstance(value, dict) or value.get("archive_version") != ARCHIVE_VERSION:
        raise InvalidProjectArchiveError("Unsupported project archive")
    manifest = cast(Mapping[str, JsonValue], value)
    project_data = _object(manifest.get("project"), "project")
    project_id = ProjectId(_string(project_data.get("id"), "project.id"))
    repository.create_project(
        owner_id=owner_id,
        project_id=project_id,
        title=_string(project_data.get("title"), "project.title"),
        created_at=_number(project_data.get("created_at"), "project.created_at"),
    )
    revisions_value = manifest.get("revisions")
    if not isinstance(revisions_value, list):
        raise InvalidProjectArchiveError("revisions must be a list")
    current: dict[DocumentType, RevisionId | None] = {kind: None for kind in DocumentType}
    for item in revisions_value:
        revision = _object(item, "revision")
        document_type = DocumentType(_string(revision.get("document_type"), "document_type"))
        payload_json = _string(revision.get("payload_json"), "payload_json")
        if content_checksum(payload_json) != _string(revision.get("checksum"), "checksum"):
            raise InvalidProjectArchiveError("Revision checksum mismatch")
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise InvalidProjectArchiveError("Revision payload must be an object")
        parent = revision.get("parent_revision_id")
        expected_parent = current[document_type]
        if parent != (str(expected_parent) if expected_parent else None):
            raise InvalidProjectArchiveError("Revision ancestry is not linear")
        committed = repository.commit_revision(
            owner_id=owner_id,
            project_id=project_id,
            document_type=document_type,
            payload=cast(Mapping[str, JsonValue], payload),
            expected_working_revision_id=expected_parent,
            actor=_string(revision.get("actor"), "actor"),
            reason=_string(revision.get("reason"), "reason"),
            revision_id=RevisionId(_string(revision.get("id"), "revision.id")),
            created_at=_number(revision.get("created_at"), "created_at"),
        )
        current[document_type] = committed.id
    selections_value = manifest.get("selections")
    if not isinstance(selections_value, list):
        raise InvalidProjectArchiveError("selections must be a list")
    for item in selections_value:
        selection = _object(item, "selection")
        accepted = selection.get("accepted_revision_id")
        if accepted is None:
            continue
        kind = DocumentType(_string(selection.get("document_type"), "document_type"))
        working = current[kind]
        if working is None:
            raise InvalidProjectArchiveError("Selection has no working revision")
        repository.accept_revision(
            owner_id=owner_id,
            project_id=project_id,
            document_type=kind,
            revision_id=RevisionId(_string(accepted, "accepted_revision_id")),
            expected_working_revision_id=working,
        )
    return project_id


def _object(value: JsonValue | None, name: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise InvalidProjectArchiveError(f"{name} must be an object")
    return value


def _string(value: JsonValue | None, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidProjectArchiveError(f"{name} must be a non-empty string")
    return value


def _number(value: JsonValue | None, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidProjectArchiveError(f"{name} must be a number")
    return float(value)
