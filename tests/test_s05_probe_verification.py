"""Downloaded S05 probe artifact verification tests."""

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


def load_verifier() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "modal" / "s05_verify_probe.py"
    spec = importlib.util.spec_from_file_location("s05_verify_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load S05 verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class S05ProbeVerificationTests(unittest.TestCase):
    def test_two_distinct_downloaded_wavs_are_verified(self) -> None:
        verifier = load_verifier()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "probe"
            run_root.mkdir()
            takes: list[dict[str, Any]] = []
            for index, seed in enumerate((10, 20)):
                path = run_root / f"take_{index}.wav"
                self._write_wav(path, frequency=220 + index * 110)
                checksum = cast(str, verifier.sha256_file(path))
                takes.append(
                    {
                        "take_id": f"take_{index}",
                        "seed": seed,
                        "relative_path": f"probe/{path.name}",
                        "size_bytes": path.stat().st_size,
                        "sha256": checksum,
                        "sample_rate_hz": 8000,
                        "channels": 1,
                    }
                )
            record = {
                "request_id": "probe",
                "request": {"duration_seconds": 1.0},
                "takes": takes,
            }
            result = verifier.verify_probe(record=record, artifact_root=root)
            self.assertTrue(result["passed"])
            self.assertEqual(2, len(result["takes"]))

            takes[1]["sha256"] = takes[0]["sha256"]
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                verifier.verify_probe(record=record, artifact_root=root)

    @staticmethod
    def _write_wav(path: Path, *, frequency: int) -> None:
        sample_rate = 8000
        samples = [
            round(10_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for index in range(sample_rate)
        ]
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


if __name__ == "__main__":
    unittest.main()
