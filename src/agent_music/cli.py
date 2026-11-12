"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys

from agent_music.config import HarnessSettings
from agent_music.runners.colab_cli import ColabCliClient
from agent_music.runners.modal_cli import ModalCliClient


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="melqivra")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the harness API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("doctor", help="Validate harness configuration")
    return parser


async def _doctor(settings: HarnessSettings) -> int:
    settings.validate()
    settings.ensure_directories()
    print(f"runner={settings.runner}")
    print(f"idle_timeout_seconds={settings.idle_timeout_seconds}")
    print(f"max_concurrency={settings.gpu_max_concurrency}")
    if settings.runner == "modal":
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
        token = await modal_client.run(modal_client.token_info_argv(), check=False)
        if token.returncode != 0:
            print("ERROR: Modal authentication is not available", file=sys.stderr)
            return 1
        print(version.stdout)
    if settings.runner == "colab":
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
        result = await colab_client.run(
            [
                executable,
                "--auth",
                settings.colab_auth_provider,
                "--config",
                str(settings.colab_cli_config),
                "version",
            ],
            check=False,
        )
        if result.returncode != 0:
            print(f"ERROR: {result.stderr}", file=sys.stderr)
            return 1
        print(result.stdout)
    print("doctor=ok")
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
    raise SystemExit(asyncio.run(_doctor(settings)))


if __name__ == "__main__":
    main()
