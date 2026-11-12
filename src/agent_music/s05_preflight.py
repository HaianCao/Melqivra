"""Pinned HeartMuLa cache and probe contracts for the bounded S05 evaluation."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from agent_music.backends.heartmula import (
    HEARTCODEC_MODEL_ID,
    HEARTMULA_BASE_MODEL_ID,
    HEARTMULA_DESCRIPTOR,
    HEARTMULA_MODEL_ID,
    HEARTMULA_SOURCE_REVISION,
)
from agent_music.domain import JsonObject, JsonValue, ProjectId, RevisionId
from agent_music.generation import MusicGenerationRequest, TakeSpec, VocalMode

HEARTMULA_SOURCE_REPOSITORY = "https://github.com/HeartMuLa/heartlib.git"
HEARTMULA_PIPELINE_SHA256 = "ca68bbad28f0fde12ad58e738527be3bb914fe86c13710108c3a15c8c361bf90"
HEARTMULA_CACHE_KEY = f"heartmula-3b-{HEARTMULA_SOURCE_REVISION[:12]}"
HEARTMULA_VOLUME_NAME = f"melqivra-s05-{HEARTMULA_CACHE_KEY}"
HEARTMULA_BASE_REPOSITORY = "HeartMuLa/HeartMuLaGen"
HEARTMULA_BASE_REVISION = "2e18e01702011f4f7dc8642b6260967df62417ea"
HEARTMULA_MUSIC_REPOSITORY = "HeartMuLa/HeartMuLa-oss-3B-happy-new-year"
HEARTMULA_MUSIC_REVISION = "00a79ac9477f64593eba2ca96a25807cb2f55be6"
HEARTMULA_CODEC_REPOSITORY = "HeartMuLa/HeartCodec-oss-20260123"
HEARTMULA_CODEC_REVISION = "f889dab0532cfa4bf459f2a3367eb6d346b8eeda"

HEARTMULA_DIRECT_DEPENDENCIES = (
    "torch==2.6.0",
    "torchaudio==2.6.0",
    "torchvision==0.21.0",
    "numpy==2.0.2",
    "torchtune==0.4.0",
    "torchao==0.9.0",
    "tqdm==4.67.1",
    "traitlets==5.7.1",
    "traittypes==0.2.3",
    "transformers==4.57.0",
    "tokenizers==0.22.1",
    "ipykernel==6.17.1",
    "einops==0.8.1",
    "accelerate==1.12.0",
    "bitsandbytes==0.49.0",
    "vector-quantize-pytorch==1.27.15",
    "modelscope==1.33.0",
    "soundfile==0.13.1",
    "huggingface-hub==0.36.0",
)

PROBE_PROMPT = "dream-pop,warm-synth,female-vocal,mid-tempo"
PROBE_LYRICS = """[Verse]
Morning light moves through the room
Quiet colors start to bloom

[Chorus]
We begin again today
Finding music on the way
"""


@dataclass(frozen=True, slots=True)
class ExpectedCacheFile:
    """One immutable file expected in the assembled model root."""

    relative_path: str
    size_bytes: int
    sha256: str | None = None


EXPECTED_CACHE_FILES = (
    ExpectedCacheFile("config.json", 101),
    ExpectedCacheFile("gen_config.json", 101),
    ExpectedCacheFile("tokenizer.json", 9_085_657),
    ExpectedCacheFile("HeartMuLa-oss-3B/config.json", 308),
    ExpectedCacheFile("HeartMuLa-oss-3B/model.safetensors.index.json", 22_579),
    ExpectedCacheFile(
        "HeartMuLa-oss-3B/model-00001-of-00004.safetensors",
        4_933_188_000,
        "4815367cdbddc0192abe3dbe81e2ae6a6e8978a39ad4dd1d26a20e445b3aeea6",
    ),
    ExpectedCacheFile(
        "HeartMuLa-oss-3B/model-00002-of-00004.safetensors",
        4_932_808_592,
        "f079416ba4c0570231f7a2830a2e95033336a8b3528d1c033a07923a25f4b5d7",
    ),
    ExpectedCacheFile(
        "HeartMuLa-oss-3B/model-00003-of-00004.safetensors",
        4_935_901_576,
        "b43cd60ff50033fa9a5e08c4eb26edff55d5e95e0a313592804ab026ed3b895d",
    ),
    ExpectedCacheFile(
        "HeartMuLa-oss-3B/model-00004-of-00004.safetensors",
        950_588_000,
        "07c5f5820c0c00f0b8ad4544ed8a24ae1888f7d8c8a5d8bb3cdaba418a39cf2f",
    ),
    ExpectedCacheFile("HeartCodec-oss/config.json", 981),
    ExpectedCacheFile("HeartCodec-oss/model.safetensors.index.json", 83_875),
    ExpectedCacheFile(
        "HeartCodec-oss/model-00001-of-00002.safetensors",
        4_930_472_472,
        "50d2c022468a0e127992d99e02117d2efbad2b10fddc8ceaf09550f154e185d4",
    ),
    ExpectedCacheFile(
        "HeartCodec-oss/model-00002-of-00002.safetensors",
        1_707_911_436,
        "6f319d0f5b31858dfe98bd8fc89d3083cbfff74ac75a0531f1a7b86949197917",
    ),
)


def build_probe_request(run_id: str) -> MusicGenerationRequest:
    """Build the smallest product-valid two-take vocal request for one operator run."""

    settings: JsonObject = {"topk": 50, "temperature": 1.0, "cfg_scale": 1.5}
    checksum_payload = {
        "language": "en",
        "vocal_mode": "vocal",
        "prompt": PROBE_PROMPT,
        "lyrics": PROBE_LYRICS,
        "duration_seconds": 20.0,
        "settings": settings,
        "seeds": [2026111201, 2026111202],
    }
    checksum = hashlib.sha256(
        json.dumps(checksum_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return MusicGenerationRequest(
        schema_version=1,
        request_id=run_id,
        project_id=ProjectId("project_s05_heartmula_probe"),
        contract_checksum=checksum,
        source_revision_ids=(RevisionId("rev_s05_operator_approved_probe"),),
        backend_id=HEARTMULA_DESCRIPTOR.backend_id,
        backend_revision=HEARTMULA_DESCRIPTOR.source_revision,
        operation="text2music",
        language="en",
        vocal_mode=VocalMode.VOCAL,
        prompt=PROBE_PROMPT,
        lyrics=PROBE_LYRICS,
        duration_seconds=20.0,
        takes=(
            TakeSpec("take_a", "faithful", 2026111201, settings),
            TakeSpec("take_b", "alternate-seed", 2026111202, settings),
        ),
    )


def validate_cache(
    model_root: Path,
    *,
    verify_hashes: bool,
    expected_files: tuple[ExpectedCacheFile, ...] = EXPECTED_CACHE_FILES,
) -> JsonObject:
    """Validate the exact assembled model tree without accepting partial cache state."""

    resolved_root = model_root.resolve()
    records: list[JsonValue] = []
    for expected in expected_files:
        path = (resolved_root / expected.relative_path).resolve()
        if resolved_root not in path.parents:
            raise ValueError(f"Cache path escaped model root: {expected.relative_path}")
        if not path.is_file():
            raise FileNotFoundError(path)
        observed_size = path.stat().st_size
        if observed_size != expected.size_bytes:
            raise ValueError(f"Cache size mismatch: {expected.relative_path}")
        observed_sha256: str | None = None
        if verify_hashes and expected.sha256 is not None:
            observed_sha256 = sha256_file(path)
            if observed_sha256 != expected.sha256:
                raise ValueError(f"Cache checksum mismatch: {expected.relative_path}")
        records.append(
            {
                "relative_path": expected.relative_path,
                "size_bytes": observed_size,
                "sha256": observed_sha256 or expected.sha256,
            }
        )
    return {
        "schema_version": 1,
        "cache_key": HEARTMULA_CACHE_KEY,
        "source_revision": HEARTMULA_SOURCE_REVISION,
        "model_ids": [
            HEARTMULA_BASE_MODEL_ID,
            HEARTMULA_MODEL_ID,
            HEARTCODEC_MODEL_ID,
        ],
        "verified_hashes": verify_hashes,
        "total_bytes": sum(item.size_bytes for item in expected_files),
        "files": records,
    }


def write_manifest_atomic(path: Path, manifest: JsonObject) -> None:
    """Commit a cache manifest without exposing a partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a file using bounded memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
