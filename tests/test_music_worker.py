"""Installed agent_music_worker boundary tests without GPU dependencies."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import agent_music_worker
from agent_music.backends.heartmula import HEARTMULA_DESCRIPTOR, HeartMuLaBackend
from agent_music.generation import MusicBackendRegistry, request_to_json
from tests.test_heartmula_backend import RecordingEngine, request


class MusicWorkerTests(unittest.TestCase):
    def test_installed_worker_executes_explicit_injected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = MusicBackendRegistry()
            registry.register(HeartMuLaBackend(RecordingEngine(), root))
            result = agent_music_worker.generate(
                {
                    "job_id": "durable_job",
                    "kind": "music.generate",
                    "payload": {"generation_request": request_to_json(request())},
                    "output_root": str(root),
                },
                registry=registry,
            )
            self.assertEqual(HEARTMULA_DESCRIPTOR.backend_id, result["backend_id"])
            self.assertEqual(2, len(result["artifacts"]))

    def test_default_worker_requires_explicit_backend_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "MUSIC_BACKEND"):
                agent_music_worker.generate(
                    {
                        "job_id": "durable_job",
                        "kind": "music.generate",
                        "payload": {"generation_request": request_to_json(request())},
                        "output_root": temporary,
                    }
                )


if __name__ == "__main__":
    unittest.main()
