"""Safe asynchronous wrapper around the official Modal CLI."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

LineCallback = Callable[[str], Awaitable[None]]
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")


class ModalCliFailureKind(StrEnum):
    """Actionable failure classes from the official Modal CLI."""

    AUTHENTICATION = "authentication"
    INCOMPATIBLE = "incompatible"
    APP_MISSING = "app_missing"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured command result."""

    returncode: int
    stdout: str
    stderr: str


class ModalCliError(RuntimeError):
    """A Modal CLI command failed without exposing job content."""

    def __init__(self, argv: Sequence[str], result: CommandResult) -> None:
        self.kind = classify_modal_failure(result)
        safe_command = " ".join(argv[:4])
        super().__init__(
            f"Modal CLI {self.kind} failure ({result.returncode}): {safe_command}; "
            f"stderr={result.stderr[-1000:]}"
        )
        self.argv = tuple(argv)
        self.result = result


class ModalCliCompatibilityError(RuntimeError):
    """The installed official CLI does not match the verified contract."""


def classify_modal_failure(result: CommandResult) -> ModalCliFailureKind:
    """Classify known Modal failures without authorizing fallback."""

    output = f"{result.stdout}\n{result.stderr}".casefold()
    auth_markers = (
        "authentication",
        "credential",
        "forbidden",
        "not authenticated",
        "token expired",
        "unauthorized",
    )
    if any(marker in output for marker in auth_markers):
        return ModalCliFailureKind.AUTHENTICATION
    incompatible_markers = (
        "incompatible",
        "no such option",
        "unknown command",
        "unsupported version",
    )
    if any(marker in output for marker in incompatible_markers):
        return ModalCliFailureKind.INCOMPATIBLE
    app_missing_markers = (
        "app does not exist",
        "app not found",
        "no app named",
    )
    if any(marker in output for marker in app_missing_markers):
        return ModalCliFailureKind.APP_MISSING
    return ModalCliFailureKind.COMMAND


def validate_modal_cli_version(output: str, expected: str) -> str:
    """Return the observed semantic version or reject an unverified CLI."""

    match = _VERSION_PATTERN.search(output)
    if match is None:
        raise ModalCliCompatibilityError("Modal CLI version output is unrecognized")
    observed = match.group(1)
    if observed != expected:
        raise ModalCliCompatibilityError(
            f"Modal CLI {observed} is incompatible; expected exactly {expected}"
        )
    return observed


class ModalCliClient:
    """Invoke only allowlisted Modal commands using argument arrays."""

    def __init__(
        self,
        *,
        executable: str,
        app_name: str,
        timeout_seconds: float,
    ) -> None:
        self._executable = executable
        self._app_name = app_name
        self._timeout_seconds = timeout_seconds
        self._command_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._current_process: asyncio.subprocess.Process | None = None
        self._current_waiter: asyncio.Task[int] | None = None

    def version_argv(self) -> list[str]:
        return [self._executable, "--version"]

    def token_info_argv(self) -> list[str]:
        return [self._executable, "token", "info"]

    def run_job_argv(
        self,
        runner_file: Path,
        request_path: Path,
        result_path: Path,
    ) -> list[str]:
        return [
            self._executable,
            "run",
            "--quiet",
            "--name",
            self._app_name,
            "--write-result",
            str(result_path),
            str(runner_file),
            "--request-file",
            str(request_path),
        ]

    def stop_argv(self) -> list[str]:
        return [self._executable, "app", "stop", "--yes", self._app_name]

    def volume_get_argv(
        self,
        volume_name: str,
        remote_path: str,
        local_directory: Path,
    ) -> list[str]:
        """Build one scoped artifact download command."""

        return [
            self._executable,
            "volume",
            "get",
            volume_name,
            remote_path,
            str(local_directory),
        ]

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run one command with bounded lifetime and cooperative termination."""

        async with self._command_lock:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **dict(environment or {})},
            )
            process_waiter = asyncio.create_task(process.wait())
            async with self._process_lock:
                self._current_process = process
                self._current_waiter = process_waiter
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            async def read_stdout() -> None:
                assert process.stdout is not None
                while line := await process.stdout.readline():
                    decoded = line.decode("utf-8", errors="replace").rstrip()
                    stdout_lines.append(decoded)
                    if on_stdout is not None:
                        await on_stdout(decoded)

            async def read_stderr() -> None:
                assert process.stderr is not None
                while line := await process.stderr.readline():
                    stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())

            readers = [
                asyncio.create_task(read_stdout()),
                asyncio.create_task(read_stderr()),
            ]
            try:
                completed, _ = await asyncio.wait(
                    {process_waiter},
                    timeout=self._timeout_seconds,
                )
                if not completed:
                    await self._terminate(process, process_waiter)
                    await process_waiter
                    await self._cancel_readers(readers)
                    raise TimeoutError(f"Modal CLI timed out: {argv[1]}")
                await asyncio.gather(*readers)
            except asyncio.CancelledError:
                await self._terminate(process, process_waiter)
                await self._cancel_readers(readers)
                raise
            finally:
                async with self._process_lock:
                    if self._current_process is process:
                        self._current_process = None
                        self._current_waiter = None

            result = CommandResult(
                returncode=int(process.returncode or 0),
                stdout="\n".join(stdout_lines),
                stderr="\n".join(stderr_lines),
            )
            if check and result.returncode != 0:
                raise ModalCliError(argv, result)
            return result

    async def cancel_current(self) -> None:
        """Terminate the active local Modal command, if any."""

        async with self._process_lock:
            process = self._current_process
            waiter = self._current_waiter
        if process is not None:
            await self._terminate(process, waiter)

    @staticmethod
    async def _terminate(
        process: asyncio.subprocess.Process,
        waiter: asyncio.Task[int] | None = None,
    ) -> None:
        if process.returncode is not None:
            return
        process_waiter = waiter or asyncio.create_task(process.wait())
        try:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return
        except ValueError:
            try:
                process.terminate()
            except ProcessLookupError:
                return
        completed, _ = await asyncio.wait({process_waiter}, timeout=10.0)
        if not completed:
            try:
                process.terminate()
            except ProcessLookupError:
                return
            completed, _ = await asyncio.wait({process_waiter}, timeout=5.0)
            if not completed:
                process.kill()
                await process_waiter

    @staticmethod
    async def _cancel_readers(readers: Sequence[asyncio.Task[None]]) -> None:
        for reader in readers:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
