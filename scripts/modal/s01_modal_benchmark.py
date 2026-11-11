"""Run the pinned S01 ACE-Step comparison on an on-demand Modal T4.

The module is invoked through ``modal run`` from WSL. Importing it defines the
research functions but performs no compute until a specific remote function is
called.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import modal  # type: ignore[import-not-found]

SOURCE_REPOSITORY = "https://github.com/ace-step/ACE-Step-1.5.git"
SOURCE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
CACHE_KEY = f"ace-step-1.5-{SOURCE_REVISION[:12]}"
VOLUME_NAME = "melqivra-s01-model-cache-dce621408bee"
REMOTE_ROOT = Path("/opt/melqivra")
REMOTE_SOURCE = Path("/opt/ACE-Step-1.5")
CACHE_ROOT = Path("/cache")
RUN_ROOT = CACHE_ROOT / "runs"
RUNTIME_PYTHON = REMOTE_SOURCE / ".venv" / "bin" / "python"

app = modal.App("melqivra-s01-modal-benchmark")
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
    local_root = Path(__file__).resolve().parents[2]
    image = (
        image.add_local_file(
            local_root / "scripts" / "colab" / "s01_runtime.py",
            str(REMOTE_ROOT / "s01_runtime.py"),
            copy=True,
        )
        .add_local_file(
            local_root / "scripts" / "colab" / "s01_benchmark.py",
            str(REMOTE_ROOT / "s01_benchmark.py"),
            copy=True,
        )
        .add_local_file(
            local_root / "scripts" / "colab" / "s01_lm_contract.py",
            str(REMOTE_ROOT / "s01_lm_contract.py"),
            copy=True,
        )
        .add_local_file(
            local_root / "docs" / "evidence" / "S01" / "model-registry.json",
            str(REMOTE_ROOT / "model-registry.json"),
            copy=True,
        )
        .add_local_file(
            local_root / "docs" / "evidence" / "S01" / "vietnamese-briefs.json",
            str(REMOTE_ROOT / "vietnamese-briefs.json"),
            copy=True,
        )
    )


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one bounded child process and preserve useful failure context."""

    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.stdout:
        print(completed.stdout[-8000:], flush=True)
    if completed.stderr:
        print(completed.stderr[-8000:], flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(argv[:4])}")
    return completed


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    volumes={str(CACHE_ROOT): volume},
    cpu=4,
    memory=16_384,
    max_containers=1,
    scaledown_window=2,
    timeout=2_400,
)
def populate_cache() -> str:
    """Populate or verify the exact model cache, then commit it atomically."""

    started = time.monotonic()
    result_path = Path("/tmp/cache-result.json")
    manifest_path = Path("/tmp/cache-manifest.json")
    checkpoint_root = Path("/tmp/checkpoints")
    try:
        _run(
            [
                str(RUNTIME_PYTHON),
                str(REMOTE_ROOT / "s01_runtime.py"),
                "--result",
                str(result_path),
                "cache-models",
                "--registry",
                str(REMOTE_ROOT / "model-registry.json"),
                "--checkpoint-root",
                str(checkpoint_root),
                "--manifest",
                str(manifest_path),
                "--persistent-cache-root",
                str(CACHE_ROOT),
            ],
            timeout=2_200,
        )
        volume.commit()
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["modal_total_seconds"] = time.monotonic() - started
        result["volume_name"] = VOLUME_NAME
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    finally:
        if checkpoint_root.exists():
            shutil.rmtree(checkpoint_root)


def _replace_checkpoints_with_volume_cache() -> None:
    checkpoint_root = REMOTE_SOURCE / "checkpoints"
    if checkpoint_root.is_symlink() or checkpoint_root.is_file():
        checkpoint_root.unlink()
    elif checkpoint_root.exists():
        shutil.rmtree(checkpoint_root)
    checkpoint_root.symlink_to(CACHE_ROOT / CACHE_KEY, target_is_directory=True)


def _benchmark_command(*, run_group: str, lm: bool, precision: str) -> list[str]:
    variant = "lm-0.6b" if lm else "dit-only"
    run_dir = RUN_ROOT / run_group / variant
    command = [
        str(RUNTIME_PYTHON),
        str(REMOTE_ROOT / "s01_benchmark.py"),
        "--source",
        str(REMOTE_SOURCE),
        "--fixtures",
        str(REMOTE_ROOT / "vietnamese-briefs.json"),
        "--fixture-id",
        "vi-020",
        "--cache-manifest",
        "/tmp/cache-manifest.json",
        "--output-dir",
        str(run_dir),
        "--result",
        str(run_dir / "benchmark.json"),
        "--run-id",
        f"{run_group}-{variant}",
        "--duration",
        "30",
        "--seed",
        "20261111",
        "--precision",
        precision,
        "--provider",
        "modal",
        "--provider-cli",
        "1.6.3",
        "--colab-cli",
        "not-applicable",
        "--session-name",
        "melqivra-s01-modal-t4",
    ]
    if lm:
        command.append("--lm")
    return command


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    volumes={str(CACHE_ROOT): volume},
    gpu="T4",
    cpu=4,
    memory=24_576,
    max_containers=1,
    scaledown_window=2,
    timeout=2_700,
)
def run_pair(run_group: str, precision: str = "upstream_default") -> str:
    """Run fixed-seed DiT-only and LM-on comparisons in one bounded T4 container."""

    if not run_group or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in run_group
    ):
        raise ValueError("run_group must contain lowercase ASCII letters, digits, or hyphens")
    if precision not in {"upstream_default", "float16", "float32"}:
        raise ValueError("Unsupported precision")
    volume.reload()
    _replace_checkpoints_with_volume_cache()

    cache_manifest = Path("/tmp/cache-manifest.json")
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
            str(REMOTE_SOURCE / "checkpoints"),
            "--manifest",
            str(cache_manifest),
            "--persistent-cache-root",
            str(CACHE_ROOT),
        ],
        timeout=900,
    )

    run_path = RUN_ROOT / run_group
    if run_path.exists():
        raise FileExistsError(f"Run group already exists: {run_group}")
    try:
        for lm in (False, True):
            _run(
                _benchmark_command(run_group=run_group, lm=lm, precision=precision),
                timeout=1_500,
            )
    finally:
        volume.commit()

    records = {}
    for variant in ("dit-only", "lm-0.6b"):
        path = run_path / variant / "benchmark.json"
        records[variant] = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(
        {
            "run_group": run_group,
            "volume_name": VOLUME_NAME,
            "records": records,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    volumes={str(CACHE_ROOT): volume},
    gpu="T4",
    cpu=4,
    memory=16_384,
    max_containers=1,
    scaledown_window=2,
    timeout=1_500,
)
def run_lm_contract(run_group: str) -> str:
    """Run only the LM conditioning phase for the vocal fixture."""

    if not run_group or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in run_group
    ):
        raise ValueError("run_group must contain lowercase ASCII letters, digits, or hyphens")
    volume.reload()
    _replace_checkpoints_with_volume_cache()
    run_path = RUN_ROOT / run_group / "lm-contract"
    if run_path.exists():
        raise FileExistsError(f"Run group already exists: {run_group}")
    result_path = run_path / "contract.json"
    try:
        _run(
            [
                str(RUNTIME_PYTHON),
                str(REMOTE_ROOT / "s01_lm_contract.py"),
                "--source",
                str(REMOTE_SOURCE),
                "--fixtures",
                str(REMOTE_ROOT / "vietnamese-briefs.json"),
                "--fixture-id",
                "vi-020",
                "--duration",
                "30",
                "--seed",
                "20261111",
                "--run-id",
                f"{run_group}-lm-contract",
                "--result",
                str(result_path),
                "--provider",
                "modal",
                "--provider-cli",
                "1.6.3",
                "--session-name",
                "melqivra-s01-modal-t4",
            ],
            timeout=1_300,
        )
    finally:
        volume.commit()
    return json.dumps(
        json.loads(result_path.read_text(encoding="utf-8")),
        ensure_ascii=False,
        sort_keys=True,
    )
