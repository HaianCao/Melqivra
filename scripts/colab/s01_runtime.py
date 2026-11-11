"""Reproducible ACE-Step S01 runtime bootstrap and model cache tooling.

This script is designed to run inside a Colab VM. Network and filesystem work
only occurs from ``main`` after an explicit subcommand is selected.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

SOURCE_REPOSITORY = "https://github.com/ace-step/ACE-Step-1.5.git"
SOURCE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
UV_VERSION = "0.13.7"
DEFAULT_ROOT = Path("/content/melqivra-s01")
DEFAULT_SOURCE = DEFAULT_ROOT / "ACE-Step-1.5"
DEFAULT_RESULT = DEFAULT_ROOT / "runtime-result.json"
CACHE_DESCRIPTOR = ".melqivra-cache.json"


class SnapshotDownload(Protocol):
    """Subset of ``huggingface_hub.snapshot_download`` used by this script."""

    def __call__(
        self,
        *,
        repo_id: str,
        revision: str,
        local_dir: str,
        allow_patterns: list[str] | None = None,
    ) -> str: ...


def emit(stage: str, **details: object) -> None:
    """Emit one machine-readable progress event without creative content."""

    print(json.dumps({"stage": stage, **details}, sort_keys=True), flush=True)


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float,
    environment: dict[str, str] | None = None,
) -> str:
    """Run one bounded command and return stripped stdout."""

    emit("command", argv=list(argv[:4]), cwd=str(cwd) if cwd else None)
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if completed.stdout:
        print(completed.stdout[-4000:], flush=True)
    if completed.stderr:
        print(completed.stderr[-4000:], file=sys.stderr, flush=True)
    return completed.stdout.strip()


def atomic_write_json(path: Path, value: object) -> None:
    """Write JSON through a sibling temporary file and atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest for a file without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_registry(path: Path) -> dict[str, Any]:
    """Load and minimally validate the tracked S01 model registry."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Model registry must be a JSON object")
    backend = value.get("backend")
    assets = value.get("assets")
    if not isinstance(backend, dict) or not isinstance(assets, list):
        raise ValueError("Model registry must contain backend and assets")
    if backend.get("source_revision") != SOURCE_REVISION:
        raise ValueError("Model registry source revision does not match the runtime pin")
    if not assets:
        raise ValueError("Model registry has no assets")
    return cast(dict[str, Any], value)


def bootstrap_runtime(*, root: Path, source: Path) -> dict[str, object]:
    """Create the exact source checkout and its locked Python 3.12 environment."""

    started = time.monotonic()
    root.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_CACHE_DIR": str(root / "uv-cache"),
    }
    run(
        [sys.executable, "-m", "pip", "install", "--quiet", f"uv=={UV_VERSION}"],
        timeout=600,
        environment=environment,
    )
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv executable was not installed")

    if not (source / ".git").is_dir():
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                SOURCE_REPOSITORY,
                str(source),
            ],
            timeout=600,
            environment=environment,
        )
    run(
        ["git", "remote", "set-url", "origin", SOURCE_REPOSITORY],
        cwd=source,
        timeout=60,
        environment=environment,
    )
    run(
        ["git", "fetch", "--depth", "1", "origin", SOURCE_REVISION],
        cwd=source,
        timeout=600,
        environment=environment,
    )
    run(
        ["git", "checkout", "--detach", "--force", SOURCE_REVISION],
        cwd=source,
        timeout=120,
        environment=environment,
    )
    actual_revision = run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        timeout=30,
        environment=environment,
    )
    if actual_revision != SOURCE_REVISION:
        raise RuntimeError(f"Unexpected source revision: {actual_revision}")

    run(
        [uv, "sync", "--frozen", "--python", "3.12"],
        cwd=source,
        timeout=2400,
        environment=environment,
    )
    runtime_python = source / ".venv" / "bin" / "python"
    versions = run(
        [
            str(runtime_python),
            "-c",
            "import json, sys, torch; "
            "print(json.dumps({'python': sys.version, 'torch': torch.__version__, "
            "'cuda': torch.version.cuda, 'cuda_available': torch.cuda.is_available()}))",
        ],
        cwd=source,
        timeout=120,
        environment=environment,
    )
    return {
        "status": "succeeded",
        "repository": SOURCE_REPOSITORY,
        "revision": actual_revision,
        "uv_version": UV_VERSION,
        "runtime": json.loads(versions.splitlines()[-1]),
        "duration_seconds": time.monotonic() - started,
    }


def _asset_groups(registry: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw_asset in registry["assets"]:
        if not isinstance(raw_asset, dict):
            raise TypeError("Every model asset must be an object")
        asset = cast(dict[str, Any], raw_asset)
        repository = asset.get("repository")
        revision = asset.get("revision")
        if not isinstance(repository, str) or not isinstance(revision, str):
            raise ValueError("Every model asset needs repository and revision strings")
        groups[(repository, revision)].append(asset)
    return dict(groups)


def _allow_patterns(assets: Iterable[dict[str, Any]]) -> list[str] | None:
    top_levels: set[str] = set()
    has_root_file = False
    for asset in assets:
        raw_path = asset.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("Every model asset needs a non-empty path")
        parts = Path(raw_path).parts
        if len(parts) == 1:
            has_root_file = True
        else:
            top_levels.add(parts[0])
    if has_root_file:
        return None
    return sorted(f"{top_level}/**" for top_level in top_levels)


def _repository_target(checkpoint_root: Path, repository: str) -> Path:
    if repository == "ACE-Step/Ace-Step1.5":
        return checkpoint_root
    return checkpoint_root / repository.rsplit("/", 1)[-1]


def _cache_key() -> str:
    return f"ace-step-1.5-{SOURCE_REVISION[:12]}"


def verify_registered_assets(
    registry: dict[str, Any], checkpoint_root: Path
) -> list[dict[str, object]]:
    """Verify every registered asset size and SHA-256 in the cache."""

    verified: list[dict[str, object]] = []
    for repository_key, assets in _asset_groups(registry).items():
        repository, revision = repository_key
        target = _repository_target(checkpoint_root, repository)
        for asset in assets:
            relative_path = Path(cast(str, asset["path"]))
            path = target / relative_path
            if not path.is_file():
                raise FileNotFoundError(f"Missing registered asset: {path}")
            expected_size = int(asset["size_bytes"])
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                raise ValueError(
                    f"Size mismatch for {asset['id']}: {actual_size} != {expected_size}"
                )
            actual_sha256 = sha256_file(path)
            expected_sha256 = str(asset["sha256"])
            if actual_sha256 != expected_sha256:
                raise ValueError(f"SHA-256 mismatch for {asset['id']}")
            verified.append(
                {
                    "id": asset["id"],
                    "repository": repository,
                    "revision": revision,
                    "relative_path": relative_path.as_posix(),
                    "size_bytes": actual_size,
                    "sha256": actual_sha256,
                }
            )
    return verified


def download_models(
    *,
    registry: dict[str, Any],
    checkpoint_root: Path,
    snapshot_download: SnapshotDownload,
) -> list[dict[str, object]]:
    """Download exact model revisions and verify all registered large assets."""

    checkpoint_root.mkdir(parents=True, exist_ok=True)
    for (repository, revision), assets in _asset_groups(registry).items():
        target = _repository_target(checkpoint_root, repository)
        target.mkdir(parents=True, exist_ok=True)
        patterns = _allow_patterns(assets)
        emit(
            "download_model",
            repository=repository,
            revision=revision,
            target=str(target),
            allow_patterns=patterns,
        )
        snapshot_download(
            repo_id=repository,
            revision=revision,
            local_dir=str(target),
            allow_patterns=patterns,
        )
    return verify_registered_assets(registry, checkpoint_root)


def _copy_cache_atomically(source: Path, destination: Path) -> None:
    """Copy a cache tree without exposing a partial destination."""

    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copytree(source, temporary)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _cache_descriptor(verified: Iterable[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "cache_key": _cache_key(),
        "source_revision": SOURCE_REVISION,
        "verified_assets": [
            {
                "id": asset["id"],
                "size_bytes": asset["size_bytes"],
                "sha256": asset["sha256"],
            }
            for asset in verified
        ],
    }


def verify_persistent_cache(
    registry: dict[str, Any], persistent_path: Path
) -> list[dict[str, object]]:
    """Reject incomplete, stale, or corrupt persistent cache directories."""

    descriptor_path = persistent_path / CACHE_DESCRIPTOR
    if not descriptor_path.is_file():
        raise FileNotFoundError(f"Persistent cache descriptor is missing: {descriptor_path}")
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict):
        raise TypeError("Persistent cache descriptor must be a JSON object")
    if descriptor.get("cache_key") != _cache_key():
        raise ValueError("Persistent cache key is stale")
    if descriptor.get("source_revision") != SOURCE_REVISION:
        raise ValueError("Persistent cache source revision is stale")
    verified = verify_registered_assets(registry, persistent_path)
    expected_descriptor = _cache_descriptor(verified)
    if descriptor != expected_descriptor:
        raise ValueError("Persistent cache descriptor does not match registered assets")
    return verified


def populate_persistent_cache(
    *,
    registry: dict[str, Any],
    checkpoint_root: Path,
    persistent_path: Path,
) -> dict[str, object]:
    """Atomically publish a verified cache, or validate an existing one."""

    started = time.monotonic()
    if persistent_path.exists():
        verified = verify_persistent_cache(registry, persistent_path)
        return {
            "status": "reused",
            "duration_seconds": time.monotonic() - started,
            "verified_assets": verified,
        }

    persistent_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = persistent_path.with_name(f".{persistent_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copytree(checkpoint_root, temporary)
        verified = verify_registered_assets(registry, temporary)
        atomic_write_json(temporary / CACHE_DESCRIPTOR, _cache_descriptor(verified))
        temporary.replace(persistent_path)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    verified = verify_persistent_cache(registry, persistent_path)
    return {
        "status": "populated",
        "duration_seconds": time.monotonic() - started,
        "verified_assets": verified,
    }


def restore_persistent_cache(
    *,
    registry_path: Path,
    checkpoint_root: Path,
    manifest_path: Path,
    persistent_cache_root: Path,
) -> dict[str, object]:
    """Verify and atomically restore the pinned cache into a clean runtime."""

    started = time.monotonic()
    registry = load_registry(registry_path)
    persistent_path = persistent_cache_root / _cache_key()
    persistent_verified = verify_persistent_cache(registry, persistent_path)
    restore_started = time.monotonic()
    if checkpoint_root.exists():
        local_verified = verify_registered_assets(registry, checkpoint_root)
        restore_status = "reused_local"
    else:
        emit("restore_cache", source=str(persistent_path), target=str(checkpoint_root))
        _copy_cache_atomically(persistent_path, checkpoint_root)
        local_verified = verify_registered_assets(registry, checkpoint_root)
        restore_status = "restored"
    restore_seconds = time.monotonic() - restore_started
    manifest: dict[str, object] = {
        "schema_version": 1,
        "cache_key": _cache_key(),
        "source_revision": SOURCE_REVISION,
        "checkpoint_root": str(checkpoint_root),
        "persistent_cache": str(persistent_path),
        "persistent_verified_assets": persistent_verified,
        "verified_assets": local_verified,
        "restore_status": restore_status,
        "restore_seconds": restore_seconds,
        "duration_seconds": time.monotonic() - started,
        "status": "succeeded",
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def cache_models(
    *,
    registry_path: Path,
    checkpoint_root: Path,
    manifest_path: Path,
    persistent_cache_root: Path | None,
) -> dict[str, object]:
    """Download, verify, manifest, and optionally persist the pinned cache."""

    started = time.monotonic()
    registry = load_registry(registry_path)
    hub = importlib.import_module("huggingface_hub")
    snapshot_download = cast(SnapshotDownload, hub.snapshot_download)
    verified = download_models(
        registry=registry,
        checkpoint_root=checkpoint_root,
        snapshot_download=snapshot_download,
    )
    cache_key = _cache_key()
    persistent_path: Path | None = None
    persistence: dict[str, object] | None = None
    if persistent_cache_root is not None:
        persistent_path = persistent_cache_root / cache_key
        emit("persist_cache", target=str(persistent_path))
        persistence = populate_persistent_cache(
            registry=registry,
            checkpoint_root=checkpoint_root,
            persistent_path=persistent_path,
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "cache_key": cache_key,
        "source_revision": SOURCE_REVISION,
        "checkpoint_root": str(checkpoint_root),
        "persistent_cache": str(persistent_path) if persistent_path else None,
        "persistence": persistence,
        "verified_assets": verified,
        "duration_seconds": time.monotonic() - started,
        "status": "succeeded",
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    bootstrap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)

    cache = subparsers.add_parser("cache-models")
    cache.add_argument("--registry", type=Path, required=True)
    cache.add_argument("--checkpoint-root", type=Path, default=DEFAULT_SOURCE / "checkpoints")
    cache.add_argument("--manifest", type=Path, required=True)
    cache.add_argument("--persistent-cache-root", type=Path)

    restore = subparsers.add_parser("restore-models")
    restore.add_argument("--registry", type=Path, required=True)
    restore.add_argument("--checkpoint-root", type=Path, default=DEFAULT_SOURCE / "checkpoints")
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--persistent-cache-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected S01 operation and always write an explicit result."""

    args = build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = bootstrap_runtime(root=args.root, source=args.source)
        elif args.command == "cache-models":
            result = cache_models(
                registry_path=args.registry,
                checkpoint_root=args.checkpoint_root,
                manifest_path=args.manifest,
                persistent_cache_root=args.persistent_cache_root,
            )
        elif args.command == "restore-models":
            result = restore_persistent_cache(
                registry_path=args.registry,
                checkpoint_root=args.checkpoint_root,
                manifest_path=args.manifest,
                persistent_cache_root=args.persistent_cache_root,
            )
        else:
            raise ValueError(f"Unsupported command: {args.command}")
        exit_code = 0
    except Exception as error:
        result = {
            "status": "failed",
            "error": f"{type(error).__name__}: {error}",
        }
        exit_code = 1
    atomic_write_json(args.result, result)
    emit("finished", status=result["status"], result_path=str(args.result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
