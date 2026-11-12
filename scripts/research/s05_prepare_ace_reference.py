"""Prepare ignored private inputs and a bounded audio-only ACE reference clip."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agent_music.audio_validation import validate_pcm16_wav  # noqa: E402

MAX_UPLOAD_BYTES = 10_000_000
MAX_DURATION_SECONDS = 60.0


def ffmpeg_command(video: Path, output: Path, *, start: float, duration: float) -> list[str]:
    """Build the fixed audio-only extraction command without shell interpolation."""

    if start < 0 or not 0 < duration < MAX_DURATION_SECONDS:
        raise ValueError("Reference clip must start at or after zero and remain under 60 seconds")
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(video),
        "-t",
        str(duration),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]


def extract_private_inputs(notebook: Path) -> tuple[str, str]:
    """Read only the exact private style and lyric assignments from the notebook."""

    document = json.loads(notebook.read_text(encoding="utf-8"))
    cells = document.get("cells")
    if not isinstance(cells, list):
        raise ValueError("Notebook has no cells array")
    values: dict[str, str] = {}
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        raw_source = cell.get("source", [])
        source = "".join(raw_source) if isinstance(raw_source, list) else str(raw_source)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in {"STYLE", "LYRICS"}:
                continue
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value.strip():
                values[target.id] = value
    if set(values) != {"STYLE", "LYRICS"}:
        raise ValueError("Notebook does not contain exact STYLE and LYRICS assignments")
    return values["STYLE"], values["LYRICS"]


def prepare_reference(
    *,
    notebook: Path,
    video: Path,
    output_root: Path,
    start_seconds: float,
    duration_seconds: float,
    consent_recorded: bool,
) -> dict[str, Any]:
    """Extract and validate private inputs while emitting only a safe manifest."""

    if output_root.exists():
        raise FileExistsError(f"Refusing to overwrite private preflight: {output_root}")
    if not video.is_file() or not notebook.is_file():
        raise FileNotFoundError("Notebook and video must both exist")
    output_root.mkdir(parents=True)
    caption, lyrics = extract_private_inputs(notebook)
    reference = output_root / "reference.wav"
    subprocess.run(
        ffmpeg_command(
            video,
            reference,
            start=start_seconds,
            duration=duration_seconds,
        ),
        check=True,
        timeout=120,
    )
    validation = validate_pcm16_wav(
        reference,
        expected_duration_seconds=duration_seconds,
    )
    if validation.get("passed") is not True:
        raise ValueError("Extracted reference audio failed validation")
    if reference.stat().st_size >= MAX_UPLOAD_BYTES:
        raise ValueError("Extracted reference audio must be smaller than 10 MB")
    private_inputs = {
        "caption": caption,
        "lyrics": lyrics,
        "tempo_bpm": 0,
    }
    private_path = output_root / "private-inputs.json"
    private_path.write_text(
        json.dumps(private_inputs, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "status": "private_preflight_ready",
        "consent_recorded": consent_recorded,
        "allowed_for_gpu": consent_recorded,
        "video_sha256": _sha256(video),
        "video_size_bytes": video.stat().st_size,
        "reference_sha256": _sha256(reference),
        "reference_size_bytes": reference.stat().st_size,
        "reference_validation": validation,
        "private_inputs_sha256": _sha256(private_path),
        "caption_sha256": _text_sha256(caption),
        "lyrics_sha256": _text_sha256(lyrics),
        "private_files": {
            "reference": "reference.wav",
            "inputs": "private-inputs.json",
        },
    }
    (output_root / "private-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--start-seconds", type=float, default=0.0)
    parser.add_argument("--duration-seconds", type=float, default=20.0)
    parser.add_argument("--consent-recorded", action="store_true")
    args = parser.parse_args()
    manifest = prepare_reference(
        notebook=args.notebook,
        video=args.video,
        output_root=args.output_root,
        start_seconds=args.start_seconds,
        duration_seconds=args.duration_seconds,
        consent_recorded=args.consent_recorded,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
