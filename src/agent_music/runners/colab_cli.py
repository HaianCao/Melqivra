"""Safe asynchronous wrapper around the official Colab CLI."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

LineCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured command result."""

    returncode: int
    stdout: str
    stderr: str


class ColabCliError(RuntimeError):
    """A Colab CLI command failed."""

    def __init__(self, argv: Sequence[str], result: CommandResult) -> None:
        safe_command = " ".join(argv[:4])
        super().__init__(
            f"Colab CLI command failed ({result.returncode}): {safe_command}; "
            f"stderr={result.stderr[-1000:]}"
        )
        self.argv = tuple(argv)
        self.result = result


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
            async with self._process_lock:
                self._current_process = process
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
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self._timeout_seconds,
                )
                await asyncio.gather(*readers)
            except TimeoutError:
                await self._terminate(process)
                await self._cancel_readers(readers)
                raise TimeoutError(f"Colab CLI timed out: {argv[-1]}") from None
            except asyncio.CancelledError:
                await self._terminate(process)
                await self._cancel_readers(readers)
                raise
            finally:
                async with self._process_lock:
                    if self._current_process is process:
                        self._current_process = None

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
        if process is not None:
            await self._terminate(process)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return
        except ValueError:
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except TimeoutError:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()

    @staticmethod
    async def _cancel_readers(readers: Sequence[asyncio.Task[None]]) -> None:
        for reader in readers:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
