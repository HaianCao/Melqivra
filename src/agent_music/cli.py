"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import asdict

from agent_music.config import HarnessSettings
from agent_music.job_store import build_job_store
from agent_music.runners.colab_cli import (
    ColabCliClient,
    ColabCliCompatibilityError,
    validate_colab_cli_version,
)
from agent_music.runners.modal_cli import (
    ModalCliClient,
    ModalCliCompatibilityError,
    validate_modal_cli_version,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="melqivra")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the harness API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("doctor", help="Validate harness configuration")
    reconcile = subparsers.add_parser(
        "reconcile-artifacts",
        help="Run one bounded PostgreSQL/S3 artifact lifecycle pass",
    )
    reconcile.add_argument("--orphan-cursor")
    return parser


async def _doctor(settings: HarnessSettings) -> int:
    settings.validate()
    settings.ensure_directories()
    print(f"runner={settings.runner}")
    print(f"job_store={settings.job_store}")
    print(f"artifact_storage={settings.artifact_storage}")
    print(f"idle_timeout_seconds={settings.idle_timeout_seconds}")
    print(f"job_lease_seconds={settings.job_lease_seconds}")
    print(f"job_heartbeat_seconds={settings.job_heartbeat_seconds}")
    print(f"job_max_attempts={settings.job_max_attempts}")
    print(f"api_auth_mode={settings.api_auth_mode}")
    print(f"api_max_active_jobs_per_user={settings.api_max_active_jobs_per_user}")
    print(f"api_max_job_payload_bytes={settings.api_max_job_payload_bytes}")
    print(f"api_max_page_size={settings.api_max_page_size}")
    print(f"api_sse_poll_seconds={settings.api_sse_poll_seconds}")
    print(f"api_sse_keepalive_seconds={settings.api_sse_keepalive_seconds}")
    print(f"api_sse_batch_size={settings.api_sse_batch_size}")
    print(f"artifact_pending_timeout_seconds={settings.artifact_pending_timeout_seconds}")
    print(f"artifact_corrupt_retention_seconds={settings.artifact_corrupt_retention_seconds}")
    print(f"artifact_orphan_grace_seconds={settings.artifact_orphan_grace_seconds}")
    print(f"artifact_published_retention_seconds={settings.artifact_published_retention_seconds}")
    print(f"artifact_reconcile_batch_limit={settings.artifact_reconcile_batch_limit}")
    print(f"artifact_reconcile_object_page_size={settings.artifact_reconcile_object_page_size}")
    print(f"max_concurrency={settings.gpu_max_concurrency}")
    if settings.runner == "modal":
        if not settings.modal_runner_file.is_file():
            print(
                f"ERROR: Modal runner file not found: {settings.modal_runner_file}",
                file=sys.stderr,
            )
            return 1
        executable = shutil.which(settings.modal_executable)
        if executable is None:
            print("ERROR: Modal CLI executable not found", file=sys.stderr)
            return 1
        modal_client = ModalCliClient(
            executable=executable,
            app_name=settings.modal_app_name,
            timeout_seconds=settings.modal_command_timeout_seconds,
        )
        version = await modal_client.run(modal_client.version_argv(), check=False)
        if version.returncode != 0:
            print(f"ERROR: {version.stderr}", file=sys.stderr)
            return 1
        try:
            observed_version = validate_modal_cli_version(
                version.stdout,
                settings.modal_cli_version,
            )
        except ModalCliCompatibilityError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        token = await modal_client.run(modal_client.token_info_argv(), check=False)
        if token.returncode != 0:
            print("ERROR: Modal authentication is not available", file=sys.stderr)
            return 1
        print(f"modal_cli_version={observed_version}")
    if settings.runner == "colab":
        if not settings.colab_remote_worker_file.is_file():
            print(
                f"ERROR: remote worker file not found: {settings.colab_remote_worker_file}",
                file=sys.stderr,
            )
            return 1
        if (
            settings.colab_cache_manifest_path is not None
            and not settings.colab_cache_manifest_path.is_file()
        ):
            print(
                f"ERROR: Colab cache manifest not found: {settings.colab_cache_manifest_path}",
                file=sys.stderr,
            )
            return 1
        executable = shutil.which(settings.colab_executable)
        if executable is None:
            print("ERROR: Colab CLI executable not found", file=sys.stderr)
            return 1
        colab_client = ColabCliClient(
            executable=executable,
            auth_provider=settings.colab_auth_provider,
            config_path=settings.colab_cli_config,
            timeout_seconds=settings.colab_command_timeout_seconds,
        )
        result = await colab_client.run(colab_client.version_argv(), check=False)
        if result.returncode != 0:
            print(f"ERROR: {result.stderr}", file=sys.stderr)
            return 1
        try:
            observed_version = validate_colab_cli_version(
                result.stdout,
                settings.colab_cli_version,
            )
        except ColabCliCompatibilityError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"colab_cli_version={observed_version}")
    if settings.job_store == "postgres":
        try:
            store = build_job_store(settings)
        except Exception:
            print("ERROR: PostgreSQL job store is unavailable", file=sys.stderr)
            return 1
        store.close()
        print("postgres_job_store=ok")
    if settings.artifact_storage == "s3":
        try:
            from agent_music.artifact_factory import build_artifact_service

            artifacts = build_artifact_service(settings)
        except Exception:
            print("ERROR: PostgreSQL/S3 artifact service is unavailable", file=sys.stderr)
            return 1
        artifacts.close()
        print("s3_artifact_service=ok")
    print("doctor=ok")
    return 0


def _reconcile_artifacts(settings: HarnessSettings, *, orphan_cursor: str | None) -> int:
    try:
        from agent_music.artifact_factory import build_artifact_reconciliation_service

        service = build_artifact_reconciliation_service(settings)
        try:
            report = service.reconcile(orphan_cursor=orphan_cursor)
        finally:
            service.close()
    except Exception:
        print("ERROR: Artifact reconciliation failed", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


def main() -> None:
    """Run CLI."""

    args = _build_parser().parse_args()
    settings = HarnessSettings.from_env()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "agent_music.api:create_app",
            host=args.host,
            port=args.port,
            reload=False,
            factory=True,
        )
        return
    if args.command == "reconcile-artifacts":
        raise SystemExit(
            _reconcile_artifacts(
                settings,
                orphan_cursor=args.orphan_cursor,
            )
        )
    raise SystemExit(asyncio.run(_doctor(settings)))


if __name__ == "__main__":
    main()
