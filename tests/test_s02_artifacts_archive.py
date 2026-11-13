"""S02 artifact and portable archive tests."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from agent_music.artifact_store import AtomicArtifactStore, CorruptArtifactError
from agent_music.domain import ArtifactId, DocumentType, ProjectId, RevisionId, UserId
from agent_music.project_archive import (
    InvalidProjectArchiveError,
    export_project_archive,
    import_project_archive,
)
from agent_music.project_repository import SqliteProjectRepository


class ArtifactStoreTests(unittest.TestCase):
    def test_atomic_write_verification_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AtomicArtifactStore(Path(temporary))
            artifact = store.put(
                project_id=ProjectId("prj_alpha"),
                artifact_id=ArtifactId("art_preview"),
                media_type="audio/wav",
                content=b"RIFF-safe",
                source_revision_ids=(RevisionId("rev_plan"),),
            )
            self.assertEqual(
                b"RIFF-safe", store.read(project_id=artifact.project_id, artifact=artifact)
            )
            with self.assertRaises(KeyError):
                store.read(project_id=ProjectId("prj_other"), artifact=artifact)
            forged_path = (
                Path(temporary)
                / "projects"
                / "prj_alpha"
                / "artifacts"
                / "batch_forged"
                / "extra"
                / str(artifact.id)
            )
            forged_path.parent.mkdir(parents=True)
            forged_path.write_bytes(b"RIFF-safe")
            forged = dataclasses.replace(
                artifact,
                relative_path=forged_path.relative_to(Path(temporary)).as_posix(),
            )
            with self.assertRaises(CorruptArtifactError):
                store.read(project_id=artifact.project_id, artifact=forged)
            (Path(temporary) / artifact.relative_path).write_bytes(b"tampered")
            with self.assertRaises(CorruptArtifactError):
                store.read(project_id=artifact.project_id, artifact=artifact)

    def test_interrupted_replace_leaves_no_partial_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AtomicArtifactStore(Path(temporary))
            with mock.patch("agent_music.artifact_store.os.replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    store.put(
                        project_id=ProjectId("prj_alpha"),
                        artifact_id=ArtifactId("art_preview"),
                        media_type="audio/wav",
                        content=b"partial",
                        source_revision_ids=(),
                    )
            self.assertEqual([], list(Path(temporary).rglob("*preview*")))


class ProjectArchiveTests(unittest.TestCase):
    def test_round_trip_preserves_ids_history_checksums_and_golden_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = SqliteProjectRepository(root / "source.db")
            target = SqliteProjectRepository(root / "target.db")
            owner = UserId("usr_alice")
            project = source.create_project(
                owner_id=owner,
                project_id=ProjectId("prj_archive"),
                title="Archive",
                created_at=1.0,
            )
            first = source.commit_revision(
                owner_id=owner,
                project_id=project.id,
                document_type=DocumentType.LYRICS,
                payload={"lines": [{"id": "lin_one", "text": "Xin chào"}]},
                expected_working_revision_id=None,
                actor="user",
                reason="initial",
                revision_id=RevisionId("rev_one"),
                created_at=2.0,
            )
            second = source.commit_revision(
                owner_id=owner,
                project_id=project.id,
                document_type=DocumentType.LYRICS,
                payload={"lines": [{"id": "lin_one", "text": "Xin chào nhé"}]},
                expected_working_revision_id=first.id,
                actor="agent",
                reason="revision",
                revision_id=RevisionId("rev_two"),
                created_at=3.0,
            )
            source.accept_revision(
                owner_id=owner,
                project_id=project.id,
                document_type=DocumentType.LYRICS,
                revision_id=first.id,
                expected_working_revision_id=second.id,
            )
            expected_revisions = source.list_revisions(
                owner_id=owner,
                project_id=project.id,
                document_type=DocumentType.LYRICS,
            )
            expected_index = source.get_index(owner_id=owner, project_id=project.id)
            archive_path = root / "project.melqivra"
            export_project_archive(
                source, owner_id=owner, project_id=project.id, destination=archive_path
            )
            source.close()
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("project.json"))
            golden = json.loads(
                (Path(__file__).parent / "fixtures" / "s02_project_archive.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(golden, manifest)
            imported_id = import_project_archive(target, owner_id=owner, source=archive_path)
            self.assertEqual(project.id, imported_id)
            actual_revisions = target.list_revisions(
                owner_id=owner,
                project_id=project.id,
                document_type=DocumentType.LYRICS,
            )
            actual_index = target.get_index(owner_id=owner, project_id=project.id)
            target.close()
            self.assertEqual(
                expected_revisions,
                actual_revisions,
            )
            self.assertEqual(expected_index, actual_index)

    def test_import_rejects_extra_entries_and_checksum_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("project.json", '{"archive_version":1}')
                archive.writestr("../escape", "bad")
            repository = SqliteProjectRepository(root / "target.db")
            with self.assertRaises(InvalidProjectArchiveError):
                import_project_archive(
                    repository, owner_id=UserId("usr_alice"), source=archive_path
                )
            repository.close()
