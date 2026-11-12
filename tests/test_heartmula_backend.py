"""CPU contract tests for the isolated HeartMuLa candidate adapter."""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from agent_music.backends.heartmula import HEARTMULA_DESCRIPTOR, HeartMuLaBackend
from agent_music.domain import ProjectId, RevisionId
from agent_music.generation import (
    GenerationContractError,
    MusicGenerationRequest,
    TakeSpec,
    VocalMode,
    execute_generation,
)


class RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, **arguments: object) -> dict[str, object]:
        self.calls.append(arguments)
        output_path = Path(str(arguments["output_path"]))
        duration = float(arguments["duration_seconds"])
        sample_rate = 8_000
        frames = round(sample_rate * duration)
        values = [
            round(12_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
            for index in range(frames)
        ]
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"".join(struct.pack("<h", value) for value in values))
        return {"peak_vram_bytes": 123}


def request() -> MusicGenerationRequest:
    settings = {"topk": 50, "temperature": 1.0, "cfg_scale": 1.5}
    return MusicGenerationRequest(
        1,
        "request_heartmula",
        ProjectId("project_s05"),
        "a" * 64,
        (RevisionId("rev_plan"),),
        HEARTMULA_DESCRIPTOR.backend_id,
        HEARTMULA_DESCRIPTOR.source_revision,
        "text2music",
        "en",
        VocalMode.VOCAL,
        "English,dream-pop,warm-synth",
        "[Verse]\nWe begin again",
        20.0,
        (
            TakeSpec("take_one", "faithful", 10, settings),
            TakeSpec("take_two", "expressive", 20, settings),
        ),
    )


class HeartMuLaBackendTests(unittest.TestCase):
    def test_exact_inputs_produce_two_valid_manifest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = RecordingEngine()
            backend = HeartMuLaBackend(engine, Path(temporary))
            result = execute_generation(backend, request(), lambda _event: None)
            self.assertEqual(2, len(result.takes))
            self.assertEqual((10, 20), tuple(item.seed for item in result.takes))
            self.assertEqual("English,dream-pop,warm-synth", engine.calls[0]["tags"])
            self.assertEqual("[Verse]\nWe begin again", engine.calls[0]["lyrics"])
            self.assertEqual("float16", result.metrics["translation"]["precision"]["music_model"])

    def test_hidden_defaults_or_instrumental_claim_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = RecordingEngine()
            backend = HeartMuLaBackend(engine, Path(temporary))
            invalid = request()
            invalid_takes = (
                TakeSpec("take_one", "faithful", 10, {"temperature": 1.0}),
                invalid.takes[1],
            )
            changed = MusicGenerationRequest(
                invalid.schema_version,
                invalid.request_id,
                invalid.project_id,
                invalid.contract_checksum,
                invalid.source_revision_ids,
                invalid.backend_id,
                invalid.backend_revision,
                invalid.operation,
                invalid.language,
                invalid.vocal_mode,
                invalid.prompt,
                invalid.lyrics,
                invalid.duration_seconds,
                invalid_takes,
            )
            with self.assertRaisesRegex(GenerationContractError, "explicitly pin"):
                execute_generation(backend, changed, lambda _event: None)
            self.assertEqual([], engine.calls)


if __name__ == "__main__":
    unittest.main()
