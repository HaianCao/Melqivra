"""Safe asynchronous wrapper around the official Colab CLI."""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

LineCallback = Callable[[str], Awaitable[None]]
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")


class ColabCliFailureKind(StrEnum):
    """Actionable failure classes from the official Colab CLI."""

    AUTHENTICATION = "authentication"
    INCOMPATIBLE = "incompatible"
    SESSION_MISSING = "session_missing"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured command result."""

    returncode: int
    stdout: str
    stderr: str


class ColabCliError(RuntimeError):
    """A Colab CLI command failed."""

    def __init__(self, argv: Sequence[str], result: CommandResult) -> None:
        self.kind = classify_colab_failure(result)
        safe_command = " ".join(argv[:4])
        super().__init__(
            f"Colab CLI {self.kind} failure ({result.returncode}): {safe_command}; "
            f"stderr={result.stderr[-1000:]}"
        )
        self.argv = tuple(argv)
        self.result = result


class ColabCliCompatibilityError(RuntimeError):
    """The installed official CLI does not match the verified contract."""


def classify_colab_failure(result: CommandResult) -> ColabCliFailureKind:
    """Classify known safe-to-act-on CLI failures without guessing fallback."""

    output = f"{result.stdout}\n{result.stderr}".casefold()
    auth_markers = (
        "authentication",
        "credential",
        "forbidden",
        "login required",
        "oauth",
        "token expired",
        "unauthorized",
    )
    if any(marker in output for marker in auth_markers):
        return ColabCliFailureKind.AUTHENTICATION
    incompatible_markers = (
        "incompatible",
        "unrecognized argument",
        "unknown command",
        "unsupported version",
    )
    if any(marker in output for marker in incompatible_markers):
        return ColabCliFailureKind.INCOMPATIBLE
    missing_markers = (
        "does not exist",
        "no session",
        "session not found",
        "unknown session",
    )
    if any(marker in output for marker in missing_markers):
        return ColabCliFailureKind.SESSION_MISSING
    return ColabCliFailureKind.COMMAND


def validate_colab_cli_version(output: str, expected: str) -> str:
    """Return the observed semantic version or reject an unverified CLI."""

    match = _VERSION_PATTERN.search(output)
    if match is None:
        raise ColabCliCompatibilityError("Colab CLI version output is unrecognized")
    observed = match.group(1)
    if observed != expected:
        raise ColabCliCompatibilityError(
            f"Colab CLI {observed} is incompatible; expected exactly {expected}"
        )
    return observed


class ColabCliClient:
    """Invoke only known Colab commands using argument arrays."""

    def __init__(
        self,
        *,
        executable: str,
        auth_provider: str,
        config_path: Path,
        timeout_seconds: float,
    ) -> None:
        self._executable = executable
        self._auth_provider = auth_provider
        self._config_path = config_path
        self._timeout_seconds = timeout_seconds
        self._command_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._current_process: asyncio.subprocess.Process | None = None
        self._current_waiter: asyncio.Task[int] | None = None

    def _base(self) -> list[str]:
        return [
            self._executable,
            "--auth",
            self._auth_provider,
            "--config",
            str(self._config_path),
        ]

    def new_argv(self, session_name: str, gpu: str) -> list[str]:
        return [*self._base(), "new", "-s", session_name, "--gpu", gpu]

    def version_argv(self) -> list[str]:
        return [self._executable, "version"]

    def status_argv(self, session_name: str) -> list[str]:
        return [*self._base(), "status", "-s", session_name]

    def stop_argv(self, session_name: str) -> list[str]:
        return [*self._base(), "stop", "-s", session_name]

    def drivemount_argv(self, session_name: str, mount_path: str) -> list[str]:
        return [
            *self._base(),
            "drivemount",
            "-s",
            session_name,
            mount_path,
        ]

    def upload_argv(
        self,
        session_name: str,
        local_path: Path,
        remote_path: str,
    ) -> list[str]:
        return [
            *self._base(),
            "upload",
            "-s",
            session_name,
            str(local_path),
            remote_path,
        ]

    def download_argv(
        self,
        session_name: str,
        remote_path: str,
        local_path: Path,
    ) -> list[str]:
        return [
            *self._base(),
            "download",
            "-s",
            session_name,
            remote_path,
            str(local_path),
        ]

    def exec_file_argv(self, session_name: str, file_path: Path) -> list[str]:
        return [
            *self._base(),
            "exec",
            "-s",
            session_name,
            "-f",
            str(file_path),
        ]

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
    ) -> CommandResult:
        """Run one CLI command, streaming stdout without shell interpolation."""

        async with self._command_lock:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
                    raise TimeoutError(f"Colab CLI timed out: {argv[-1]}")
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
                raise ColabCliError(argv, result)
            return result

    async def cancel_current(self) -> None:
        """Terminate the active local Colab command, if any."""

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
