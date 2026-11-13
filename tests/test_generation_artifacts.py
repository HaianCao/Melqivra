"""Immutable S05 generation artifact import tests."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from agent_music.artifact_store import AtomicArtifactStore, CorruptArtifactError
from agent_music.domain import ProjectId, RevisionId, UserId
from agent_music.generation import (
    GeneratedTake,
    GenerationBatchResult,
    GenerationContractError,
    MusicBackendDescriptor,
    MusicGenerationRequest,
    TakeSpec,
    VocalMode,
)
from agent_music.generation_artifacts import import_generation_batch


class GenerationArtifactImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptor = MusicBackendDescriptor(
            backend_id="heartmula_3b",
            source_revision="source-pin",
            model_ids=("music@pin", "codec@pin"),
            license_ids=("Apache-2.0",),
            operations=frozenset({"text2music"}),
            supported_languages=frozenset({"en"}),
            supports_vocals=True,
            supports_instrumental=False,
            min_duration_seconds=1.0,
            max_duration_seconds=30.0,
        )
        self.request = MusicGenerationRequest(
            1,
            "request_import",
            ProjectId("project_import"),
            "a" * 64,
            (
                RevisionId("rev_brief"),
                RevisionId("rev_blueprint"),
                RevisionId("rev_lyrics"),
                RevisionId("rev_plan"),
            ),
            "heartmula_3b",
            "source-pin",
            "text2music",
            "en",
            VocalMode.VOCAL,
            "Private creative prompt",
            "Private approved lyrics",
            1.0,
            (
                TakeSpec("take_one", "faithful", 11, {"temperature": 0.9}),
                TakeSpec("take_two", "alternate", 22, {"temperature": 1.0}),
            ),
        )

    def test_two_take_batch_is_atomic_idempotent_and_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote"
            result = self._write_result(remote)
            store = AtomicArtifactStore(root / "artifacts")
            imported = self._import(store, remote, result)

            self.assertEqual(2, len(imported.takes))
            self.assertEqual(imported, self._import(store, remote, result))
            batch_directory = (
                root
                / "artifacts"
                / "projects"
                / str(self.request.project_id)
                / "artifacts"
                / imported.batch_id
            )
            self.assertEqual(4, len(tuple(batch_directory.iterdir())))
            for item in imported.takes:
                self.assertEqual(
                    result.takes[
                        [take.take_id for take in result.takes].index(
                            str(item.manifest.settings["backend_take_id"])
                        )
                    ].sha256,
                    item.audio_artifact.sha256,
                )
                manifest_bytes = store.read(
                    project_id=self.request.project_id,
                    artifact=item.manifest_artifact,
                )
                manifest = json.loads(manifest_bytes)
                self.assertEqual(self.request.contract_checksum, manifest["contract_checksum"])
                self.assertEqual("source-pin", manifest["backend_revision"])
                self.assertEqual(str(item.take.id), manifest["take_id"])
                self.assertNotIn("Private creative prompt", manifest_bytes.decode("utf-8"))
                self.assertNotIn("Private approved lyrics", manifest_bytes.decode("utf-8"))

    def test_integrity_failure_commits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote"
            result = self._write_result(remote)
            broken = GenerationBatchResult(
                result.backend_id,
                result.backend_revision,
                result.contract_checksum,
                (
                    GeneratedTake(
                        *(
                            result.takes[0].take_id,
                            result.takes[0].seed,
                            result.takes[0].relative_path,
                            result.takes[0].media_type,
                            result.takes[0].size_bytes,
                            "f" * 64,
                            result.takes[0].duration_seconds,
                            result.takes[0].sample_rate_hz,
                            result.takes[0].channels,
                            result.takes[0].validation,
                        )
                    ),
                    result.takes[1],
                ),
                result.metrics,
            )
            store_root = root / "artifacts"
            with self.assertRaisesRegex(GenerationContractError, "integrity"):
                self._import(AtomicArtifactStore(store_root), remote, broken)
            self.assertEqual([], list(store_root.rglob("batch_*")))

    def test_interrupted_batch_publish_leaves_no_partial_take(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote"
            result = self._write_result(remote)
            store_root = root / "artifacts"
            store = AtomicArtifactStore(store_root)
            with mock.patch("agent_music.artifact_store.os.replace", side_effect=OSError("stop")):
                with self.assertRaisesRegex(OSError, "stop"):
                    self._import(store, remote, result)
            project_artifacts = store_root / "projects" / str(self.request.project_id) / "artifacts"
            self.assertEqual([], list(project_artifacts.iterdir()))

    def test_idempotent_replay_rejects_tampered_committed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote"
            result = self._write_result(remote)
            store = AtomicArtifactStore(root / "artifacts")
            imported = self._import(store, remote, result)
            audio = imported.takes[0].audio_artifact
            (root / "artifacts" / audio.relative_path).write_bytes(b"tampered")
            with self.assertRaises(CorruptArtifactError):
                self._import(store, remote, result)

    def _import(
        self,
        store: AtomicArtifactStore,
        remote: Path,
        result: GenerationBatchResult,
    ):  # type: ignore[no-untyped-def]
        return import_generation_batch(
            request=self.request,
            result=result,
            descriptor=self.descriptor,
            owner_id=UserId("usr_test"),
            plan_revision_id=RevisionId("rev_plan"),
            remote_artifact_root=remote,
            artifact_publisher=store,
            environment={"provider": "modal", "gpu": "Tesla T4"},
        )

    def _write_result(self, remote: Path) -> GenerationBatchResult:
        remote.mkdir()
        generated: list[GeneratedTake] = []
        for index, spec in enumerate(self.request.takes):
            path = remote / f"{spec.take_id}.wav"
            self._write_wav(path, frequency=220 + index * 110)
            content = path.read_bytes()
            generated.append(
                GeneratedTake(
                    spec.take_id,
                    spec.seed,
                    path.name,
                    "audio/wav",
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    1.0,
                    8_000,
                    2,
                    {"passed": True, "silent": False, "clipped": False},
                )
            )
        return GenerationBatchResult(
            self.request.backend_id,
            self.request.backend_revision,
            self.request.contract_checksum,
            tuple(generated),
            {"worker_seconds": 1.0},
        )

    @staticmethod
    def _write_wav(path: Path, *, frequency: int) -> None:
        sample_rate = 8_000
        frames = []
        for index in range(sample_rate):
            sample = round(8_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.append(struct.pack("<hh", sample, sample))
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"".join(frames))


if __name__ == "__main__":
    unittest.main()
