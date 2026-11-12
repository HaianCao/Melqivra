"""Deterministic production WAV validation tests."""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from agent_music.audio_validation import validate_pcm16_wav


def write_wav(path: Path, *, seconds: float, amplitude: float) -> None:
    sample_rate = 8_000
    frames = round(sample_rate * seconds)
    values = [
        round(32_767 * amplitude * math.sin(2 * math.pi * 200 * index / sample_rate))
        for index in range(frames)
    ]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<h", value) for value in values))


class AudioValidationTests(unittest.TestCase):
    def test_valid_pcm16_audio_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "take.wav"
            write_wav(path, seconds=1.0, amplitude=0.5)
            result = validate_pcm16_wav(path, expected_duration_seconds=1.0)
            self.assertTrue(result["passed"])
            self.assertFalse(result["silent"])
            self.assertFalse(result["clipped"])

    def test_silent_clipped_and_wrong_duration_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            silent = root / "silent.wav"
            clipped = root / "clipped.wav"
            short = root / "short.wav"
            write_wav(silent, seconds=1.0, amplitude=0.0)
            write_wav(clipped, seconds=1.0, amplitude=1.0)
            write_wav(short, seconds=0.25, amplitude=0.5)
            self.assertFalse(validate_pcm16_wav(silent, expected_duration_seconds=1.0)["passed"])
            self.assertFalse(validate_pcm16_wav(clipped, expected_duration_seconds=1.0)["passed"])
            self.assertFalse(
                validate_pcm16_wav(
                    short,
                    expected_duration_seconds=2.0,
                    duration_tolerance_seconds=0.1,
                )["passed"]
            )


if __name__ == "__main__":
    unittest.main()
