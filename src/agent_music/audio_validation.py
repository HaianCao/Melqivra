"""Small deterministic PCM WAV validation used at the backend boundary."""

from __future__ import annotations

import array
import wave
from pathlib import Path

from agent_music.domain import JsonObject


def validate_pcm16_wav(
    path: Path,
    *,
    expected_duration_seconds: float,
    duration_tolerance_seconds: float | None = None,
) -> JsonObject:
    """Validate a non-empty, non-clipped PCM16 WAV with the expected duration."""

    if expected_duration_seconds <= 0:
        raise ValueError("Expected duration must be positive")
    if not path.is_file():
        raise FileNotFoundError(path)
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate_hz = wav.getframerate()
        sample_width_bytes = wav.getsampwidth()
        frames = wav.getnframes()
        compression = wav.getcomptype()
        raw = wav.readframes(frames)
    if compression != "NONE" or sample_width_bytes != 2:
        raise ValueError("Backend artifact must be uncompressed PCM16 WAV")
    if channels <= 0 or sample_rate_hz <= 0 or frames <= 0:
        raise ValueError("Backend artifact has invalid WAV metadata")
    samples = array.array("h")
    samples.frombytes(raw)
    if not samples:
        raise ValueError("Backend artifact has no audio samples")
    absolute_peak = max(abs(sample) for sample in samples)
    duration_seconds = frames / sample_rate_hz
    tolerance = (
        duration_tolerance_seconds
        if duration_tolerance_seconds is not None
        else max(1.0, expected_duration_seconds * 0.05)
    )
    silent = absolute_peak < 4
    clipped = absolute_peak >= 32_767
    duration_matches = abs(duration_seconds - expected_duration_seconds) <= tolerance
    return {
        "passed": not silent and not clipped and duration_matches,
        "duration_seconds": duration_seconds,
        "duration_matches": duration_matches,
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
        "sample_width_bytes": sample_width_bytes,
        "finite_samples": True,
        "silent": silent,
        "clipped": clipped,
        "absolute_peak": absolute_peak / 32_768,
    }
