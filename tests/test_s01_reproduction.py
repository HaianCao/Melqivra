"""Exact-input and near-identical WAV reproduction verification tests."""

from __future__ import annotations

import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from agent_music.s01_reproduction import (
    CapabilityArtifactError,
    ReproductionMismatchError,
    compare_reference_artifact,
    compare_repaint_artifact,
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

    def test_reference_output_must_differ_from_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            generated = Path(temporary) / "generated.wav"
            self._wav(source)
            self._wav(generated)
            with self.assertRaisesRegex(CapabilityArtifactError, "byte-identical"):
                compare_reference_artifact(source=source, generated=generated)

    def test_repaint_verifier_detects_localized_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            generated = Path(temporary) / "generated.wav"
            source_samples = array("h", ((index % 200) - 100 for index in range(2000)))
            generated_samples = array("h", source_samples)
            for index in range(800, 1200):
                generated_samples[index] = -generated_samples[index]
            for path, samples in ((source, source_samples), (generated, generated_samples)):
                with wave.open(str(path), "wb") as stream:
                    stream.setnchannels(2)
                    stream.setsampwidth(2)
                    stream.setframerate(100)
                    stream.writeframes(samples.tobytes())
            result = compare_repaint_artifact(
                source=source,
                generated=generated,
                repaint_start_seconds=3.0,
                repaint_end_seconds=7.0,
                boundary_margin_seconds=0.5,
            )
            self.assertTrue(result["localized_change_observed"])
            self.assertTrue(result["surrounding_audio_preserved"])


if __name__ == "__main__":
    unittest.main()
