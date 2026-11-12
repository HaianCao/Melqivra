"""Bounded HeartMuLa S05 app with CPU cache population and one T4 job path."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast

import modal

if modal.is_local():
    local_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(local_root / "src"))

from agent_music.backends.heartmula import HEARTMULA_SOURCE_REVISION
from agent_music.s05_preflight import (
    HEARTMULA_BASE_REPOSITORY,
    HEARTMULA_BASE_REVISION,
    HEARTMULA_CACHE_KEY,
    HEARTMULA_CODEC_REPOSITORY,
    HEARTMULA_CODEC_REVISION,
    HEARTMULA_DIRECT_DEPENDENCIES,
    HEARTMULA_MUSIC_REPOSITORY,
    HEARTMULA_MUSIC_REVISION,
    HEARTMULA_PIPELINE_SHA256,
    HEARTMULA_SOURCE_REPOSITORY,
    HEARTMULA_VOLUME_NAME,
    sha256_file,
    validate_cache,
    write_manifest_atomic,
)

SOURCE_ROOT = Path("/opt/heartlib")
PRODUCT_ROOT = Path("/opt/melqivra")
REMOTE_WORKER = PRODUCT_ROOT / "src" / "agent_music" / "remote" / "worker.py"
CACHE_ROOT = Path("/cache")
MODEL_ROOT = CACHE_ROOT / "models" / HEARTMULA_CACHE_KEY
STAGING_ROOT = CACHE_ROOT / "staging"
RUN_ROOT = CACHE_ROOT / "runs"
MANIFEST_PATH = MODEL_ROOT / "cache-manifest.json"

app = modal.App("melqivra-s05-heartmula")
volume = modal.Volume.from_name(HEARTMULA_VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git", "libsndfile1")
    .pip_install("uv==0.13.7")
    .run_commands(
        f"git clone --filter=blob:none --no-checkout {HEARTMULA_SOURCE_REPOSITORY} {SOURCE_ROOT}",
        f"git -C {SOURCE_ROOT} fetch --depth 1 origin {HEARTMULA_SOURCE_REVISION}",
        f"git -C {SOURCE_ROOT} checkout --detach --force {HEARTMULA_SOURCE_REVISION}",
        "uv pip install --system --index-url https://download.pytorch.org/whl/cu124 "
        "torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0",
        "uv pip install --system " + " ".join(HEARTMULA_DIRECT_DEPENDENCIES[3:]),
        f"uv pip install --system --no-deps {SOURCE_ROOT}",
    )
    .env(
        {
            "PYTHONPATH": str(PRODUCT_ROOT / "src"),
            "MUSIC_BACKEND": "heartmula_3b",
            "HEARTMULA_MODEL_ROOT": str(MODEL_ROOT),
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
)

if modal.is_local():
    image = image.add_local_dir(
        str(local_root / "src" / "agent_music"),
        remote_path=str(PRODUCT_ROOT / "src" / "agent_music"),
        copy=True,
    ).add_local_dir(
        str(local_root / "src" / "agent_music_worker"),
        remote_path=str(PRODUCT_ROOT / "src" / "agent_music_worker"),
        copy=True,
    )


def _image_contract() -> dict[str, Any]:
    music_generation = importlib.import_module("heartlib.pipelines.music_generation")
    torch = importlib.import_module("torch")

    pipeline_path = Path(str(music_generation.__file__))
    observed_pipeline_sha256 = sha256_file(pipeline_path)
    if observed_pipeline_sha256 != HEARTMULA_PIPELINE_SHA256:
        raise RuntimeError("Installed HeartMuLa pipeline does not match the source pin")
    return {
        "source_repository": HEARTMULA_SOURCE_REPOSITORY,
        "source_revision": HEARTMULA_SOURCE_REVISION,
        "pipeline_sha256": observed_pipeline_sha256,
        "python": sys.version,
        "torch_cuda": torch.version.cuda,
        "direct_dependencies": list(HEARTMULA_DIRECT_DEPENDENCIES),
        "installed_versions": {
            name: importlib.metadata.version(name)
            for name in (
                "heartlib",
                "torch",
                "torchaudio",
                "torchvision",
                "transformers",
                "tokenizers",
                "accelerate",
                "bitsandbytes",
                "huggingface-hub",
            )
        },
    }


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    cpu=2,
    memory=4_096,
    max_containers=1,
    scaledown_window=2,
    timeout=300,
)
def verify_image_contract() -> str:
    """Build/import the exact runtime without downloading weights or using a GPU."""

    return json.dumps(_image_contract(), ensure_ascii=False, sort_keys=True)


def _download_snapshot(
    *,
    repository: str,
    revision: str,
    target: Path,
    allow_patterns: list[str],
) -> None:
    huggingface_hub = importlib.import_module("huggingface_hub")

    huggingface_hub.snapshot_download(
        repo_id=repository,
        revision=revision,
        local_dir=target,
        allow_patterns=allow_patterns,
        max_workers=4,
    )


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    volumes={str(CACHE_ROOT): volume},
    cpu=4,
    memory=8_192,
    max_containers=1,
    scaledown_window=2,
    timeout=3_600,
)
def populate_cache() -> str:
    """Download, checksum, and atomically publish the exact HeartMuLa model root."""

    started = time.monotonic()
    volume.reload()
    if MODEL_ROOT.exists():
        manifest = validate_cache(MODEL_ROOT, verify_hashes=True)
        manifest["cache_status"] = "verified_existing"
        manifest["elapsed_seconds"] = time.monotonic() - started
        return json.dumps(manifest, ensure_ascii=False, sort_keys=True)

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    staging = (STAGING_ROOT / f"{HEARTMULA_CACHE_KEY}-{uuid.uuid4().hex}").resolve()
    if staging.parent != STAGING_ROOT.resolve():
        raise RuntimeError("Invalid HeartMuLa staging path")
    staging.mkdir(parents=False, exist_ok=False)
    try:
        _download_snapshot(
            repository=HEARTMULA_BASE_REPOSITORY,
            revision=HEARTMULA_BASE_REVISION,
            target=staging,
            allow_patterns=["config.json", "gen_config.json", "tokenizer.json"],
        )
        _download_snapshot(
            repository=HEARTMULA_MUSIC_REPOSITORY,
            revision=HEARTMULA_MUSIC_REVISION,
            target=staging / "HeartMuLa-oss-3B",
            allow_patterns=["config.json", "model*.safetensors", "model.safetensors.index.json"],
        )
        _download_snapshot(
            repository=HEARTMULA_CODEC_REPOSITORY,
            revision=HEARTMULA_CODEC_REVISION,
            target=staging / "HeartCodec-oss",
            allow_patterns=["config.json", "model*.safetensors", "model.safetensors.index.json"],
        )
        for metadata in staging.rglob(".cache"):
            if metadata.is_dir() and staging in metadata.resolve().parents:
                shutil.rmtree(metadata)
        manifest = validate_cache(staging, verify_hashes=True)
        manifest["cache_status"] = "downloaded_and_verified"
        manifest["elapsed_seconds"] = time.monotonic() - started
        write_manifest_atomic(staging / MANIFEST_PATH.name, manifest)
        MODEL_ROOT.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(MODEL_ROOT)
        volume.commit()
        return json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    except BaseException:
        if staging.exists() and staging.parent == STAGING_ROOT.resolve():
            shutil.rmtree(staging)
        raise


def _execute_worker(request_json: str) -> dict[str, Any]:
    request = json.loads(request_json)
    if not isinstance(request, dict) or request.get("kind") != "music.generate":
        raise ValueError("S05 HeartMuLa app accepts only music.generate")
    request["output_root"] = str(RUN_ROOT)
    with tempfile.TemporaryDirectory(prefix="melqivra-s05-heartmula-") as temporary:
        root = Path(temporary)
        request_path = root / "job.json"
        result_path = root / "result.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(REMOTE_WORKER),
                "--request",
                str(request_path),
                "--result",
                str(result_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ},
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
        return_code = process.wait()
        if not result_path.is_file():
            raise RuntimeError(f"HeartMuLa worker exited {return_code} without a result")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise TypeError("HeartMuLa worker result must be an object")
        return cast(dict[str, Any], result)


def _request_output_root(request_json: str) -> Path:
    request = json.loads(request_json)
    payload = request.get("payload", {})
    generation_request = payload.get("generation_request", {}) if isinstance(payload, dict) else {}
    request_id = (
        generation_request.get("request_id") if isinstance(generation_request, dict) else None
    )
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("music.generate request_id is required")
    output = (RUN_ROOT / request_id).resolve()
    if RUN_ROOT.resolve() not in output.parents:
        raise ValueError("music.generate output escaped the run root")
    return output


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    volumes={str(CACHE_ROOT): volume},
    gpu="T4",
    cpu=4,
    memory=16_384,
    max_containers=1,
    scaledown_window=2,
    timeout=1_800,
)
def run_gpu_job(request_json: str) -> dict[str, Any]:
    """Execute one approved two-take request with no fallback or retry."""

    volume.reload()
    if not MANIFEST_PATH.is_file():
        raise RuntimeError("HeartMuLa cache manifest is missing")
    recorded_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if recorded_manifest.get("verified_hashes") is not True:
        raise RuntimeError("HeartMuLa cache was not checksum-verified")
    validate_cache(MODEL_ROOT, verify_hashes=False)
    output_root = _request_output_root(request_json)
    if output_root.exists():
        raise FileExistsError(f"HeartMuLa run already exists: {output_root.name}")
    result = _execute_worker(request_json)
    if result.get("status") == "succeeded":
        volume.commit()
    else:
        if output_root.exists() and output_root.parent == RUN_ROOT.resolve():
            shutil.rmtree(output_root)
        volume.commit()
    return result


@app.local_entrypoint()  # type: ignore[untyped-decorator]
def main(request_file: str) -> str:
    """Use the product file transport while keeping creative content out of argv."""

    request_json = Path(request_file).read_text(encoding="utf-8")
    result = run_gpu_job.remote(request_json)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
