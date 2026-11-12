"""Exact-input and near-identical WAV reproduction verification tests."""

from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from agent_music.s01_reproduction import (
    ReproductionMismatchError,
    compare_reproduction,
    sha256_file,
)


class ReproductionTests(unittest.TestCase):
    def _wav(self, path: Path) -> None:
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48_000)
            stream.writeframes(b"\x01\x00\x01\x00" * 480)

    def _record(self, path: Path) -> dict[str, object]:
        return {
            "result": "passed",
            "request": {
                "fixture_id": "vi-014",
                "task_type": "text2music",
                "duration_seconds": 0.01,
                "seed": 20261111,
                "diffusion_steps": 8,
                "shift": 3.0,
                "batch_size": 1,
                "precision": "upstream_default",
                "lm_requested": False,
            },
            "source": {"repository": "repo", "revision": "revision"},
            "assets": [
                {
                    "id": "model",
                    "repository": "weights",
                    "revision": "weights-revision",
                    "sha256": "asset-sha",
                }
            ],
            "audio_validation": {
                "duration_seconds": 0.01,
                "sample_rate_hz": 48_000,
                "channels": 2,
                "finite_samples": True,
                "silent": False,
                "clipped": False,
            },
            "artifacts": [
                {
                    "path": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            ],
        }

    def test_exact_request_and_downloaded_wav_contract_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "sample.wav"
            self._wav(artifact)
            record = self._record(artifact)
            result = compare_reproduction(
                modal_record=record,
                colab_record=record,
                local_artifact=artifact,
                transfer_seconds=0.5,
            )
            self.assertTrue(result["artifact_checksum_match"])
            self.assertEqual("passed", result["status"])

    def test_input_or_download_corruption_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "sample.wav"
            self._wav(artifact)
            modal = self._record(artifact)
            colab = self._record(artifact)
            modal["request"]["seed"] = 1  # type: ignore[index]
            with self.assertRaisesRegex(ReproductionMismatchError, "seed"):
                compare_reproduction(
                    modal_record=modal,
                    colab_record=colab,
                    local_artifact=artifact,
                    transfer_seconds=0.5,
                )
            modal = self._record(artifact)
            artifact.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ReproductionMismatchError, "checksum"):
                compare_reproduction(
                    modal_record=modal,
                    colab_record=colab,
                    local_artifact=artifact,
                    transfer_seconds=0.5,
                )


if __name__ == "__main__":
    unittest.main()
