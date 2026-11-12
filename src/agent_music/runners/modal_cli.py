"""Safe asynchronous wrapper around the official Modal CLI."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LineCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured command result."""

    returncode: int
    stdout: str
    stderr: str


class ModalCliError(RuntimeError):
    """A Modal CLI command failed without exposing job content."""

    def __init__(self, argv: Sequence[str], result: CommandResult) -> None:
        safe_command = " ".join(argv[:4])
        super().__init__(
            f"Modal CLI command failed ({result.returncode}): {safe_command}; "
            f"stderr={result.stderr[-1000:]}"
        )
        self.argv = tuple(argv)
        self.result = result


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
                await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
                await asyncio.gather(*readers)
            except TimeoutError:
                await self._terminate(process)
                await self._cancel_readers(readers)
                raise TimeoutError(f"Modal CLI timed out: {argv[1]}") from None
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
                raise ModalCliError(argv, result)
            return result

    async def cancel_current(self) -> None:
        """Terminate the active local Modal command, if any."""

        async with self._process_lock:
            process = self._current_process
        if process is not None:
            await self._terminate(process)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _cancel_readers(readers: Sequence[asyncio.Task[None]]) -> None:
        for reader in readers:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
