"""One-shot Modal T4 probe for the pinned ACE-Step float32 reference path."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import modal

PRODUCT_ROOT = Path("/opt/melqivra/product")
LOCAL_ROOT = Path(__file__).resolve().parents[2] if modal.is_local() else None
sys.path.insert(0, str(LOCAL_ROOT / "src" if LOCAL_ROOT is not None else PRODUCT_ROOT))

from agent_music.s05_ace_probe import (  # noqa: E402
    ACE_FLOAT32_REPOSITORY,
    ACE_FLOAT32_REVISION,
    ACE_UPSTREAM_BASE_REVISION,
    PROBE_CPU_CORES,
    PROBE_MAX_FUNCTION_SECONDS,
    PROBE_MAX_TRANSFER_BYTES,
    PROBE_MEMORY_GIB,
    decode_exact_utf8,
    validate_probe_authorization,
)

SOURCE_REPOSITORY = ACE_FLOAT32_REPOSITORY
SOURCE_REVISION = ACE_FLOAT32_REVISION
UPSTREAM_BASE_REVISION = ACE_UPSTREAM_BASE_REVISION
VOLUME_NAME = "melqivra-s01-model-cache-dce621408bee"
CACHE_KEY = f"ace-step-1.5-{UPSTREAM_BASE_REVISION[:12]}"
CACHE_ROOT = Path("/cache")
REMOTE_ROOT = Path("/opt/melqivra")
REMOTE_SOURCE = Path("/opt/ACE-Step-1.5-float32-fix")
RUNTIME_PYTHON = REMOTE_SOURCE / ".venv" / "bin" / "python"
MAX_PRIVATE_FILE_BYTES = PROBE_MAX_TRANSFER_BYTES

app = modal.App("melqivra-s05-ace-reference-probe")
volume = modal.Volume.from_name(VOLUME_NAME)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "git", "libsndfile1")
    .pip_install("uv==0.13.7")
    .run_commands(
        f"git clone --filter=blob:none --no-checkout {SOURCE_REPOSITORY} {REMOTE_SOURCE}",
        f"git -C {REMOTE_SOURCE} fetch --depth 1 origin {SOURCE_REVISION}",
        f"git -C {REMOTE_SOURCE} checkout --detach --force {SOURCE_REVISION}",
        f"cd {REMOTE_SOURCE} && uv sync --frozen --python 3.12",
    )
)

if modal.is_local():
    assert LOCAL_ROOT is not None
    image = (
        image.add_local_file(
            LOCAL_ROOT / "scripts" / "colab" / "s01_runtime.py",
            str(REMOTE_ROOT / "s01_runtime.py"),
            copy=True,
        )
        .add_local_file(
            LOCAL_ROOT / "scripts" / "colab" / "s01_benchmark.py",
            str(REMOTE_ROOT / "s01_benchmark.py"),
            copy=True,
        )
        .add_local_file(
            LOCAL_ROOT / "docs" / "evidence" / "S01" / "model-registry.json",
            str(REMOTE_ROOT / "model-registry.json"),
            copy=True,
        )
        .add_local_file(
            LOCAL_ROOT / "src" / "agent_music" / "s05_ace_probe.py",
            str(PRODUCT_ROOT / "agent_music" / "s05_ace_probe.py"),
            copy=True,
        )
    )


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout)[-4000:]
        raise RuntimeError(f"Bounded ACE probe command failed: {tail}")
    return completed


def _restore_cache() -> Path:
    volume.reload()
    checkpoint_root = REMOTE_SOURCE / "checkpoints"
    if checkpoint_root.is_symlink() or checkpoint_root.is_file():
        checkpoint_root.unlink()
    elif checkpoint_root.exists():
        shutil.rmtree(checkpoint_root)
    checkpoint_root.symlink_to(CACHE_ROOT / CACHE_KEY, target_is_directory=True)
    manifest = Path("/tmp/cache-manifest.json")
    _run(
        [
            str(RUNTIME_PYTHON),
            str(REMOTE_ROOT / "s01_runtime.py"),
            "--result",
            "/tmp/restore-result.json",
            "restore-models",
            "--registry",
            str(REMOTE_ROOT / "model-registry.json"),
            "--checkpoint-root",
            str(checkpoint_root),
            "--manifest",
            str(manifest),
            "--persistent-cache-root",
            str(CACHE_ROOT),
        ],
        timeout=300,
    )
    return manifest


def _assert_t4() -> str:
    completed = _run(
        [
            "nvidia-smi",
            "--query-gpu=name",
            "--format=csv,noheader",
        ],
        timeout=30,
    )
    gpu_name = completed.stdout.strip()
    if "T4" not in gpu_name:
        raise RuntimeError(f"Expected Tesla T4, received {gpu_name}")
    return gpu_name


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    volumes={str(CACHE_ROOT): volume},
    gpu="T4",
    cpu=PROBE_CPU_CORES,
    memory=PROBE_MEMORY_GIB * 1024,
    max_containers=1,
    scaledown_window=2,
    timeout=PROBE_MAX_FUNCTION_SECONDS,
)
def run_probe(
    run_id: str,
    reference_audio: bytes,
    private_inputs_json: str,
    authorization_json: str,
) -> str:
    """Execute exactly one 20-second take with no retry or fallback."""

    if not run_id.replace("-", "").isalnum():
        raise ValueError("Run ID must contain only letters, digits, and hyphens")
    if not reference_audio or len(reference_audio) >= MAX_PRIVATE_FILE_BYTES:
        raise ValueError("Reference audio must be non-empty and smaller than 10 MB")
    reference_sha256 = hashlib.sha256(reference_audio).hexdigest()
    private_inputs_sha256 = hashlib.sha256(private_inputs_json.encode("utf-8")).hexdigest()
    authorization = json.loads(authorization_json)
    if not isinstance(authorization, dict):
        raise ValueError("Authorization must be a JSON object")
    validate_probe_authorization(
        cast(dict[str, Any], authorization),
        reference_sha256=reference_sha256,
        private_inputs_sha256=private_inputs_sha256,
    )
    gpu_name = _assert_t4()
    manifest = _restore_cache()
    reference_path = Path("/tmp/reference.wav")
    inputs_path = Path("/tmp/private-inputs.json")
    reference_path.write_bytes(reference_audio)
    inputs_path.write_text(private_inputs_json, encoding="utf-8")
    run_root = CACHE_ROOT / "runs" / "s05" / run_id
    if run_root.exists():
        raise FileExistsError(f"Run already exists: {run_id}")
    result_path = run_root / "benchmark.json"
    started = time.monotonic()
    try:
        _run(
            [
                str(RUNTIME_PYTHON),
                str(REMOTE_ROOT / "s01_benchmark.py"),
                "--source",
                str(REMOTE_SOURCE),
                "--source-repository",
                SOURCE_REPOSITORY,
                "--source-revision",
                SOURCE_REVISION,
                "--inputs-file",
                str(inputs_path),
                "--cache-manifest",
                str(manifest),
                "--output-dir",
                str(run_root),
                "--result",
                str(result_path),
                "--run-id",
                run_id,
                "--duration",
                "20",
                "--seed",
                "12345",
                "--precision",
                "float32",
                "--reference-audio",
                str(reference_path),
                "--thinking-without-lm",
                "--vocal-language",
                "vi",
                "--provider",
                "modal",
                "--provider-cli",
                "1.6.3",
                "--colab-cli",
                "not-applicable",
                "--session-name",
                "melqivra-s05-ace-reference-t4",
            ],
            timeout=600,
        )
    finally:
        volume.commit()
    record = json.loads(result_path.read_text(encoding="utf-8"))
    return json.dumps(
        {
            "status": "succeeded",
            "run_id": run_id,
            "gpu_name": gpu_name,
            "elapsed_seconds": time.monotonic() - started,
            "volume_name": VOLUME_NAME,
            "record_path": str(result_path.relative_to(CACHE_ROOT)).replace("\\", "/"),
            "record": record,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@app.local_entrypoint()  # type: ignore[untyped-decorator]
def main(
    run_id: str,
    reference_audio: str,
    private_inputs: str,
    authorization: str,
) -> None:
    """Validate private local files before making the one remote call."""

    reference_path = Path(reference_audio)
    inputs_path = Path(private_inputs)
    authorization_path = Path(authorization)
    reference_bytes = reference_path.read_bytes()
    private_json, private_inputs_sha256 = decode_exact_utf8(inputs_path.read_bytes())
    authorization_json = authorization_path.read_text(encoding="utf-8")
    authorization_value = json.loads(authorization_json)
    if not isinstance(authorization_value, dict):
        raise ValueError("Authorization must be a JSON object")
    validate_probe_authorization(
        cast(dict[str, Any], authorization_value),
        reference_sha256=hashlib.sha256(reference_bytes).hexdigest(),
        private_inputs_sha256=private_inputs_sha256,
    )
    print(
        run_probe.remote(run_id, reference_bytes, private_json, authorization_json),
        flush=True,
    )
