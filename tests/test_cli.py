"""Public CLI diagnostics and import-safety tests."""

from __future__ import annotations

import asyncio
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.cli import _doctor
from agent_music.config import HarnessSettings


class DoctorTests(unittest.TestCase):
    def test_fake_runner_doctor_creates_only_configured_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = HarnessSettings(
                database_path=root / "state" / "harness.db",
                artifact_root=root / "artifacts",
                colab_cli_config=root / "colab" / "sessions.json",
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = asyncio.run(_doctor(settings))

            self.assertEqual(0, exit_code)
            self.assertIn("runner=fake", output.getvalue())
            self.assertIn("doctor=ok", output.getvalue())
            self.assertTrue((root / "state").is_dir())
            self.assertTrue((root / "artifacts").is_dir())
            self.assertTrue((root / "colab").is_dir())

    def test_colab_doctor_fails_cleanly_when_cli_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = HarnessSettings(
                database_path=root / "state" / "harness.db",
                artifact_root=root / "artifacts",
                runner="colab",
                colab_cli_config=root / "colab" / "sessions.json",
            )
            error = io.StringIO()

            with patch("agent_music.cli.shutil.which", return_value=None):
                with contextlib.redirect_stderr(error):
                    exit_code = asyncio.run(_doctor(settings))

            self.assertEqual(1, exit_code)
            self.assertIn("Colab CLI executable not found", error.getvalue())

    def test_imports_do_not_create_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-c", "import agent_music; import agent_music.api"],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual([], list(Path(directory).iterdir()))
