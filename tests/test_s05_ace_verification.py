"""Downloaded ACE reference-probe verification tests."""

from __future__ import annotations

import importlib.util
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from agent_music.s05_ace_probe import ACE_FLOAT32_REVISION


def load_verifier() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "modal" / "s05_verify_ace_reference.py"
    spec = importlib.util.spec_from_file_location("s05_verify_ace_reference", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load ACE reference verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class S05AceVerificationTests(unittest.TestCase):
    def test_exact_float32_t4_artifact_is_verified(self) -> None:
        verifier = load_verifier()
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "result.wav"
            self._write_wav(audio)
            record: dict[str, Any] = {
                "result": "passed",
                "run_id": "ace-test",
                "source": {"revision": ACE_FLOAT32_REVISION},
                "environment": {"provider": "modal", "gpu_name": "Tesla T4"},
                "request": {
                    "duration_seconds": 1.0,
                    "precision": "float32",
                    "observed_precision": "float32",
                    "seed": 12345,
                    "vocal_language": "vi",
                    "reference_audio_sha256": "a" * 64,
                },
                "artifacts": [
                    {
                        "size_bytes": audio.stat().st_size,
                        "sha256": cast(str, verifier.sha256_file(audio)),
                    }
                ],
            }
            result = verifier.verify_ace_reference_probe(
                record=record,
                audio_path=audio,
                expected_run_id="ace-test",
            )
            self.assertTrue(result["passed"])
            self.assertEqual("float32", result["observed_precision"])

            cast(dict[str, Any], record["artifacts"][0])["sha256"] = "b" * 64
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                verifier.verify_ace_reference_probe(
                    record=record,
                    audio_path=audio,
                    expected_run_id="ace-test",
                )

    @staticmethod
    def _write_wav(path: Path) -> None:
        sample_rate = 8000
        samples = [
            round(10_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
            for index in range(sample_rate)
        ]
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


if __name__ == "__main__":
    unittest.main()
