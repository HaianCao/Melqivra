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

from agent_music.artifact_reconciliation import ArtifactReconciliationReport
from agent_music.cli import _build_parser, _doctor, _reconcile_artifacts
from agent_music.config import HarnessSettings


class DoctorTests(unittest.TestCase):
    def test_primary_cli_uses_melqivra_identity(self) -> None:
        self.assertEqual("melqivra", _build_parser().prog)

    def test_fake_runner_doctor_creates_only_configured_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = HarnessSettings(
                database_path=root / "state" / "harness.db",
                artifact_root=root / "artifacts",
                runner="fake",
                colab_cli_config=root / "colab" / "sessions.json",
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = asyncio.run(_doctor(settings))

            self.assertEqual(0, exit_code)
            self.assertIn("runner=fake", output.getvalue())
            self.assertIn("job_store=sqlite", output.getvalue())
            self.assertIn("artifact_storage=filesystem", output.getvalue())
            self.assertIn("job_lease_seconds=60.0", output.getvalue())
            self.assertIn("job_heartbeat_seconds=15.0", output.getvalue())
            self.assertIn("job_max_attempts=3", output.getvalue())
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

    def test_modal_doctor_fails_cleanly_when_cli_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = HarnessSettings(
                database_path=root / "state" / "harness.db",
                artifact_root=root / "artifacts",
                runner="modal",
            )
            error = io.StringIO()

            with patch("agent_music.cli.shutil.which", return_value=None):
                with contextlib.redirect_stderr(error):
                    exit_code = asyncio.run(_doctor(settings))

            self.assertEqual(1, exit_code)
            self.assertIn("Modal CLI executable not found", error.getvalue())

    def test_postgres_doctor_checks_store_without_printing_dsn(self) -> None:
        settings = HarnessSettings(
            runner="fake",
            job_store="postgres",
            postgres_dsn="postgresql://secret@database/melqivra",
        )
        output = io.StringIO()

        with patch("agent_music.cli.build_job_store") as build_store:
            with contextlib.redirect_stdout(output):
                exit_code = asyncio.run(_doctor(settings))

        self.assertEqual(0, exit_code)
        self.assertIn("job_store=postgres", output.getvalue())
        self.assertIn("postgres_job_store=ok", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        build_store.return_value.close.assert_called_once_with()

    def test_postgres_doctor_sanitizes_connection_failure(self) -> None:
        settings = HarnessSettings(
            runner="fake",
            job_store="postgres",
            postgres_dsn="postgresql://secret@database/melqivra",
        )
        error = io.StringIO()

        with patch(
            "agent_music.cli.build_job_store",
            side_effect=RuntimeError("postgresql://secret@database/melqivra"),
        ):
            with contextlib.redirect_stderr(error):
                exit_code = asyncio.run(_doctor(settings))

        self.assertEqual(1, exit_code)
        self.assertIn("PostgreSQL job store is unavailable", error.getvalue())
        self.assertNotIn("secret", error.getvalue())

    def test_s3_doctor_checks_service_without_printing_endpoint(self) -> None:
        settings = HarnessSettings(
            runner="fake",
            job_store="postgres",
            postgres_dsn="postgresql://secret@database/melqivra",
            artifact_storage="s3",
            s3_endpoint_url="https://secret-storage.example.test",
            s3_bucket="melqivra-artifacts",
        )
        output = io.StringIO()

        with (
            patch("agent_music.cli.build_job_store") as build_store,
            patch("agent_music.artifact_factory.build_artifact_service") as build_artifacts,
            contextlib.redirect_stdout(output),
        ):
            exit_code = asyncio.run(_doctor(settings))

        self.assertEqual(0, exit_code)
        self.assertIn("artifact_storage=s3", output.getvalue())
        self.assertIn("s3_artifact_service=ok", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        build_store.return_value.close.assert_called_once_with()
        build_artifacts.return_value.close.assert_called_once_with()

    def test_artifact_reconciliation_cli_is_bounded_and_sanitized(self) -> None:
        settings = HarnessSettings(
            runner="fake",
            job_store="postgres",
            postgres_dsn="postgresql://secret@database/melqivra",
            artifact_storage="s3",
            s3_endpoint_url="https://secret-storage.example.test",
            s3_bucket="melqivra-artifacts",
        )
        report = ArtifactReconciliationReport(
            recovered_pending_batches=1,
            deleted_pending_batches=2,
            deleted_corrupt_batches=3,
            resumed_deleting_batches=4,
            expired_published_batches=5,
            deleted_orphan_objects=6,
            deferred_orphan_objects=7,
            next_orphan_cursor="next-page",
        )
        output = io.StringIO()
        with (
            patch(
                "agent_music.artifact_factory.build_artifact_reconciliation_service"
            ) as build_service,
            contextlib.redirect_stdout(output),
        ):
            build_service.return_value.reconcile.return_value = report
            exit_code = _reconcile_artifacts(settings, orphan_cursor="prior-page")

        self.assertEqual(0, exit_code)
        self.assertIn('"deleted_orphan_objects": 6', output.getvalue())
        self.assertIn('"next_orphan_cursor": "next-page"', output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        build_service.return_value.reconcile.assert_called_once_with(orphan_cursor="prior-page")
        build_service.return_value.close.assert_called_once_with()

        error = io.StringIO()
        with (
            patch(
                "agent_music.artifact_factory.build_artifact_reconciliation_service",
                side_effect=RuntimeError("secret-storage.example.test"),
            ),
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(1, _reconcile_artifacts(settings, orphan_cursor=None))
        self.assertIn("Artifact reconciliation failed", error.getvalue())
        self.assertNotIn("secret-storage", error.getvalue())

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
