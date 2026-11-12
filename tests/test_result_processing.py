"""Fenced generation completion and restart-safe retrieval tests."""

from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import tempfile
import time
import unittest
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_music.artifact_store import AtomicArtifactStore
from agent_music.config import HarnessSettings
from agent_music.domain import ProjectId, RevisionId
from agent_music.generation import (
    GeneratedTake,
    GenerationBatchResult,
    MusicBackendDescriptor,
    MusicGenerationRequest,
    TakeSpec,
    VocalMode,
    request_to_json,
    result_to_json,
)
from agent_music.models import GpuJob, JobState, RunnerState
from agent_music.result_processing import GenerationResultProcessor
from agent_music.runners.base import EventCallback
from agent_music.scheduler import GpuHarness
from agent_music.store import SqliteJobStore


async def wait_until(predicate: Callable[[], bool], deadline_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Condition was not reached")
        await asyncio.sleep(0.005)


class CompletedGenerationRunner:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self._state = RunnerState.STOPPED

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        return None

    async def ensure_ready(self) -> None:
        self._state = RunnerState.READY

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        _ = job
        self._state = RunnerState.BUSY
        await on_event({"type": "stage", "stage": "transfer_complete", "progress": 0.9})
        await asyncio.sleep(0)
        self._state = RunnerState.READY
        return self._result

    async def cancel(self, job_id: str) -> None:
        _ = job_id

    async def stop(self) -> None:
        self._state = RunnerState.STOPPED


class ResultProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_committed_only_after_import_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                runner="fake",
                scheduler_poll_seconds=0.01,
                idle_timeout_seconds=1.0,
            )
            request, descriptor, runner_result = self._fixture(settings.transfer_root)
            processor = GenerationResultProcessor(
                artifact_store=AtomicArtifactStore(settings.artifact_root),
                transfer_root=settings.transfer_root,
                descriptors=(descriptor,),
            )
            harness = GpuHarness(
                settings,
                runner=CompletedGenerationRunner(runner_result),
                result_processor=processor,
            )
            await harness.start()
            job = harness.enqueue(
                user_id="user_s05",
                kind="music.generate",
                payload={
                    "generation_request": request_to_json(request),
                    "plan_revision_id": "rev_plan",
                },
                idempotency_key="approved-contract",
            )
            await wait_until(lambda: harness.store.get(job.id).state is JobState.SUCCEEDED)
            completed = harness.store.get(job.id)
            await harness.close()

            assert completed.result is not None
            serialized = str(completed.result)
            self.assertNotIn("Private prompt", serialized)
            self.assertNotIn("Private lyrics", serialized)
            self.assertNotIn("_transfer_id", completed.result)
            takes = completed.result["generation"]["takes"]
            self.assertEqual(2, len(takes))

            reopened = SqliteJobStore(settings.database_path)
            recovered = reopened.get(job.id)
            reopened.close()
            self.assertEqual(completed.result, recovered.result)
            for take in takes:
                audio = take["audio_artifact"]
                path = settings.artifact_root / audio["relative_path"]
                self.assertTrue(path.is_file())
                self.assertEqual(audio["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    async def test_import_failure_marks_job_failed_without_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                runner="fake",
                scheduler_poll_seconds=0.01,
                idle_timeout_seconds=1.0,
            )
            request, descriptor, runner_result = self._fixture(settings.transfer_root)
            transfer = settings.transfer_root / str(runner_result["_transfer_id"])
            next(transfer.glob("*.wav")).unlink()
            harness = GpuHarness(
                settings,
                runner=CompletedGenerationRunner(runner_result),
                result_processor=GenerationResultProcessor(
                    artifact_store=AtomicArtifactStore(settings.artifact_root),
                    transfer_root=settings.transfer_root,
                    descriptors=(descriptor,),
                ),
            )
            await harness.start()
            job = harness.enqueue(
                user_id="user_s05",
                kind="music.generate",
                payload={
                    "generation_request": request_to_json(request),
                    "plan_revision_id": "rev_plan",
                },
            )
            await wait_until(lambda: harness.store.get(job.id).state is JobState.FAILED)
            failed = harness.store.get(job.id)
            await harness.close()

            self.assertIsNone(failed.result)
            self.assertIn("FileNotFoundError", failed.error or "")
            self.assertEqual([], list(settings.artifact_root.rglob("batch_*")))

    def _fixture(
        self,
        transfer_root: Path,
    ) -> tuple[MusicGenerationRequest, MusicBackendDescriptor, dict[str, Any]]:
        descriptor = MusicBackendDescriptor(
            "test_backend",
            "source-pin",
            ("model@pin",),
            ("Apache-2.0",),
            frozenset({"text2music"}),
            frozenset({"en"}),
            True,
            False,
            1.0,
            10.0,
        )
        request = MusicGenerationRequest(
            1,
            "request_restart",
            ProjectId("project_restart"),
            "b" * 64,
            (
                RevisionId("rev_brief"),
                RevisionId("rev_blueprint"),
                RevisionId("rev_lyrics"),
                RevisionId("rev_plan"),
            ),
            descriptor.backend_id,
            descriptor.source_revision,
            "text2music",
            "en",
            VocalMode.VOCAL,
            "Private prompt",
            "Private lyrics",
            1.0,
            (
                TakeSpec("take_one", "one", 1, {}),
                TakeSpec("take_two", "two", 2, {}),
            ),
        )
        transfer_id = "transfer_fixture"
        transfer = transfer_root / transfer_id
        transfer.mkdir(parents=True)
        takes: list[GeneratedTake] = []
        for index, spec in enumerate(request.takes):
            path = transfer / f"{spec.take_id}.wav"
            self._write_wav(path, 220 + index * 110)
            content = path.read_bytes()
            takes.append(
                GeneratedTake(
                    spec.take_id,
                    spec.seed,
                    path.name,
                    "audio/wav",
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    1.0,
                    8_000,
                    1,
                    {"passed": True, "silent": False, "clipped": False},
                )
            )
        batch = GenerationBatchResult(
            descriptor.backend_id,
            descriptor.source_revision,
            request.contract_checksum,
            tuple(takes),
            {"worker_seconds": 1.0},
        )
        return (
            request,
            descriptor,
            {
                "status": "succeeded",
                "result": result_to_json(batch),
                "_transfer_id": transfer_id,
                "_environment": {"provider": "fake-transfer"},
            },
        )

    @staticmethod
    def _write_wav(path: Path, frequency: int) -> None:
        sample_rate = 8_000
        samples = [
            round(8_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for index in range(sample_rate)
        ]
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


if __name__ == "__main__":
    unittest.main()
