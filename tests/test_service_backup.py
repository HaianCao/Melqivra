"""Bounded PostgreSQL/S3 service backup and restore contracts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_music.object_store import ObjectInfo, ObjectPage
from agent_music.service_backup import (
    BackupDatabaseSnapshot,
    BackupObjectContract,
    DockerPostgresArchiveTool,
    LocalPostgresArchiveTool,
    ServiceArchiveError,
    ServiceBackupManager,
    ServiceBackupManifest,
)


class MemoryObjectStore:
    def __init__(self, operation_log: list[str] | None = None) -> None:
        self.closed = False
        self.items: dict[str, tuple[bytes, str, str]] = {}
        self.operation_log = operation_log if operation_log is not None else []

    def close(self) -> None:
        self.closed = True

    def put_immutable(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        sha256: str,
    ) -> ObjectInfo:
        self.operation_log.append(f"put:{key}")
        if hashlib.sha256(content).hexdigest() != sha256:
            raise ValueError("checksum mismatch")
        prior = self.items.get(key)
        contract = (content, media_type, sha256)
        if prior is not None and prior != contract:
            raise RuntimeError("immutable conflict")
        self.items[key] = contract
        return self._info(key)

    def stat(self, key: str) -> ObjectInfo | None:
        return self._info(key) if key in self.items else None

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        content = self.items[key][0]
        if max_bytes is not None and len(content) > max_bytes:
            raise RuntimeError("oversized")
        return content

    def delete(self, key: str) -> None:
        self.items.pop(key, None)

    def list(
        self,
        prefix: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ObjectPage:
        keys = sorted(key for key in self.items if key.startswith(prefix))
        start = int(cursor or "0")
        selected = keys[start : start + limit]
        next_index = start + len(selected)
        return ObjectPage(
            tuple(self._info(key, listing=True) for key in selected),
            str(next_index) if next_index < len(keys) else None,
        )

    def _info(self, key: str, *, listing: bool = False) -> ObjectInfo:
        content, media_type, sha256 = self.items[key]
        return ObjectInfo(
            key=key,
            size_bytes=len(content),
            sha256=None if listing else sha256,
            media_type=None if listing else media_type,
            last_modified_at=1.0,
        )


class FakeCatalog:
    def __init__(self, snapshot: BackupDatabaseSnapshot) -> None:
        self.value = snapshot
        self.empty = True
        self.verified: ServiceBackupManifest | None = None
        self.operation_log: list[str] = []

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[BackupDatabaseSnapshot]:
        self.operation_log.append("snapshot:start")
        try:
            yield self.value
        finally:
            self.operation_log.append("snapshot:finish")

    def require_empty_restore_target(self) -> None:
        self.operation_log.append("database:preflight")
        if not self.empty:
            raise ServiceArchiveError("target is not empty")

    def verify_restored_manifest(self, manifest: ServiceBackupManifest) -> None:
        self.operation_log.append("database:verify")
        self.verified = manifest


class FakeArchiveTool:
    def __init__(self, operation_log: list[str] | None = None) -> None:
        self.operation_log = operation_log if operation_log is not None else []
        self.dumped = False
        self.restored = False

    def dump(self, dsn: str, destination: Path) -> None:
        _ = dsn
        self.operation_log.append("database:dump")
        destination.write_bytes(b"PGDMP\x01stable-test-database")
        self.dumped = True

    def restore(self, dsn: str, source: Path) -> None:
        _ = dsn
        self.operation_log.append("database:restore")
        if not source.read_bytes().startswith(b"PGDMP"):
            raise RuntimeError("invalid archive")
        self.restored = True


def _contract(key: str, content: bytes, *, state: str = "published") -> BackupObjectContract:
    return BackupObjectContract(
        object_key=key,
        media_type="audio/wav",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        state=state,
    )


def _manager(
    catalog: FakeCatalog,
    store: MemoryObjectStore,
    tool: FakeArchiveTool,
    *,
    operation_log: list[str] | None = None,
) -> ServiceBackupManager:
    if operation_log is not None:
        catalog.operation_log = operation_log
        store.operation_log = operation_log
        tool.operation_log = operation_log
    return ServiceBackupManager(
        postgres_dsn="postgresql://credentials-must-not-appear",
        catalog=catalog,
        object_store=store,
        archive_tool=tool,
        max_database_bytes=1_024,
        max_objects=10,
        max_total_object_bytes=1_024,
        object_page_size=2,
        clock=lambda: 1_800_000_000.0,
        backup_id_factory=lambda: "bkp_0123456789abcdef0123456789abcdef",
    )


def _rewrite_manifest(archive: Path, payload: dict[str, Any], *, canonical: bool = True) -> None:
    if canonical:
        content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    else:
        content = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    (archive / "manifest.json").write_bytes(content)
    (archive / "manifest.sha256").write_text(
        f"{digest}  manifest.json\n",
        encoding="ascii",
    )


class ServiceBackupTests(unittest.TestCase):
    def test_round_trip_is_atomic_checksummed_and_restores_bytes_before_metadata(self) -> None:
        published = b"published-audio"
        complete_pending = b"complete-pending"
        missing_pending = b"missing-pending"
        contracts = (
            _contract("artifacts/tenant/project/batch/published", published),
            _contract(
                "artifacts/tenant/project/batch/complete-pending",
                complete_pending,
                state="pending",
            ),
            _contract(
                "artifacts/tenant/project/batch/missing-pending",
                missing_pending,
                state="pending",
            ),
        )
        source = MemoryObjectStore()
        for contract, content in zip(contracts[:2], (published, complete_pending), strict=True):
            source.put_immutable(
                key=contract.object_key,
                content=content,
                media_type=contract.media_type,
                sha256=contract.sha256,
            )
        source_catalog = FakeCatalog(BackupDatabaseSnapshot(8, contracts))
        source_tool = FakeArchiveTool()

        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "service-backup"
            report = _manager(source_catalog, source, source_tool).create_backup(archive)

            self.assertTrue(source_tool.dumped)
            self.assertEqual(2, report.object_count)
            self.assertEqual(1, report.skipped_incomplete_pending_objects)
            self.assertEqual(8, report.database_schema_version)
            self.assertEqual(
                report.manifest_sha256,
                (archive / "manifest.sha256").read_text(encoding="ascii").split()[0],
            )
            manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("postgresql-custom", manifest["database"]["format"])
            self.assertNotIn("credentials-must-not-appear", json.dumps(manifest))
            self.assertTrue(all(item["blob_path"].endswith(".bin") for item in manifest["objects"]))

            operation_log: list[str] = []
            target = MemoryObjectStore(operation_log)
            target_catalog = FakeCatalog(BackupDatabaseSnapshot(8, ()))
            target_tool = FakeArchiveTool(operation_log)
            restored = _manager(
                target_catalog,
                target,
                target_tool,
                operation_log=operation_log,
            ).restore_backup(archive)

        self.assertTrue(target_tool.restored)
        self.assertEqual(report.backup_id, restored.backup_id)
        self.assertEqual(published, target.get(contracts[0].object_key))
        self.assertEqual(complete_pending, target.get(contracts[1].object_key))
        self.assertIsNotNone(target_catalog.verified)
        restore_index = operation_log.index("database:restore")
        self.assertTrue(
            all(
                operation_log.index(f"put:{contract.object_key}") < restore_index
                for contract in contracts[:2]
            )
        )

    def test_tampered_blob_fails_before_target_mutation(self) -> None:
        content = b"verified"
        contract = _contract("artifacts/tenant/project/batch/audio", content)
        source = MemoryObjectStore()
        source.put_immutable(
            key=contract.object_key,
            content=content,
            media_type=contract.media_type,
            sha256=contract.sha256,
        )
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "backup"
            _manager(
                FakeCatalog(BackupDatabaseSnapshot(8, (contract,))),
                source,
                FakeArchiveTool(),
            ).create_backup(archive)
            manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
            blob = archive.joinpath(*manifest["objects"][0]["blob_path"].split("/"))
            blob.write_bytes(b"tampered")

            target = MemoryObjectStore()
            catalog = FakeCatalog(BackupDatabaseSnapshot(8, ()))
            tool = FakeArchiveTool()
            with self.assertRaisesRegex(ServiceArchiveError, "integrity verification"):
                _manager(catalog, target, tool).restore_backup(archive)

        self.assertEqual({}, target.items)
        self.assertFalse(tool.restored)
        self.assertIsNone(catalog.verified)

    def test_restore_refuses_nonempty_database_or_unrelated_object_prefix(self) -> None:
        content = b"verified"
        contract = _contract("artifacts/tenant/project/batch/audio", content)
        source = MemoryObjectStore()
        source.put_immutable(
            key=contract.object_key,
            content=content,
            media_type=contract.media_type,
            sha256=contract.sha256,
        )
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "backup"
            _manager(
                FakeCatalog(BackupDatabaseSnapshot(8, (contract,))),
                source,
                FakeArchiveTool(),
            ).create_backup(archive)

            database_catalog = FakeCatalog(BackupDatabaseSnapshot(8, ()))
            database_catalog.empty = False
            with self.assertRaisesRegex(ServiceArchiveError, "not empty"):
                _manager(
                    database_catalog,
                    MemoryObjectStore(),
                    FakeArchiveTool(),
                ).restore_backup(archive)

            target = MemoryObjectStore()
            unrelated = b"unrelated"
            target.put_immutable(
                key="artifacts/unrelated/object",
                content=unrelated,
                media_type="audio/wav",
                sha256=hashlib.sha256(unrelated).hexdigest(),
            )
            with self.assertRaisesRegex(ServiceArchiveError, "not empty or compatible"):
                _manager(
                    FakeCatalog(BackupDatabaseSnapshot(8, ())),
                    target,
                    FakeArchiveTool(),
                ).restore_backup(archive)

    def test_restore_resumes_exact_objects_across_multiple_pages(self) -> None:
        first_content = b"first"
        second_content = b"second"
        contracts = (
            _contract("artifacts/tenant/project/batch/first", first_content),
            _contract("artifacts/tenant/project/batch/second", second_content),
        )
        source = MemoryObjectStore()
        for contract, content in zip(contracts, (first_content, second_content), strict=True):
            source.put_immutable(
                key=contract.object_key,
                content=content,
                media_type=contract.media_type,
                sha256=contract.sha256,
            )
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "backup"
            _manager(
                FakeCatalog(BackupDatabaseSnapshot(8, contracts)),
                source,
                FakeArchiveTool(),
            ).create_backup(archive)
            target = MemoryObjectStore()
            for contract, content in zip(contracts, (first_content, second_content), strict=True):
                target.put_immutable(
                    key=contract.object_key,
                    content=content,
                    media_type=contract.media_type,
                    sha256=contract.sha256,
                )
            manager = ServiceBackupManager(
                postgres_dsn="postgresql://database/restore",
                catalog=FakeCatalog(BackupDatabaseSnapshot(8, ())),
                object_store=target,
                archive_tool=FakeArchiveTool(),
                max_database_bytes=1_024,
                max_objects=10,
                max_total_object_bytes=1_024,
                object_page_size=1,
            )
            restored = manager.restore_backup(archive)

        self.assertEqual(2, restored.object_count)

    def test_manifest_validation_rejects_tampering_and_noncanonical_input(self) -> None:
        content = b"verified"
        contract = _contract("artifacts/tenant/project/batch/audio", content)
        source = MemoryObjectStore()
        source.put_immutable(
            key=contract.object_key,
            content=content,
            media_type=contract.media_type,
            sha256=contract.sha256,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            _manager(
                FakeCatalog(BackupDatabaseSnapshot(8, (contract,))),
                source,
                FakeArchiveTool(),
            ).create_backup(original)
            original_payload = json.loads((original / "manifest.json").read_text(encoding="utf-8"))
            cases: tuple[tuple[str, Any, str, bool], ...] = (
                ("extra", {**original_payload, "unexpected": True}, "fields", True),
                (
                    "version",
                    {**original_payload, "schema_version": 2},
                    "unsupported",
                    True,
                ),
                (
                    "bad_id",
                    {**original_payload, "backup_id": "unsafe"},
                    "backup ID",
                    True,
                ),
                (
                    "negative_time",
                    {**original_payload, "created_at": -1},
                    "creation time",
                    True,
                ),
                (
                    "unsafe_blob",
                    {
                        **original_payload,
                        "objects": [{**original_payload["objects"][0], "blob_path": "../escape"}],
                    },
                    "object path",
                    True,
                ),
                ("noncanonical", original_payload, "not canonical", False),
            )
            for name, payload, message, canonical in cases:
                with self.subTest(name=name):
                    archive = root / name
                    shutil.copytree(original, archive)
                    _rewrite_manifest(archive, payload, canonical=canonical)
                    with self.assertRaisesRegex(ServiceArchiveError, message):
                        _manager(
                            FakeCatalog(BackupDatabaseSnapshot(8, ())),
                            MemoryObjectStore(),
                            FakeArchiveTool(),
                        ).restore_backup(archive)

            checksum_archive = root / "checksum"
            shutil.copytree(original, checksum_archive)
            (checksum_archive / "manifest.sha256").write_text(
                f"{'0' * 64}  manifest.json\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ServiceArchiveError, "manifest integrity"):
                _manager(
                    FakeCatalog(BackupDatabaseSnapshot(8, ())),
                    MemoryObjectStore(),
                    FakeArchiveTool(),
                ).restore_backup(checksum_archive)

    def test_archive_bounds_and_metadata_are_enforced_before_publication(self) -> None:
        first_content = b"first"
        second_content = b"second"
        contracts = (
            _contract("artifacts/tenant/project/batch/first", first_content),
            _contract("artifacts/tenant/project/batch/second", second_content),
        )
        store = MemoryObjectStore()
        for contract, content in zip(contracts, (first_content, second_content), strict=True):
            store.put_immutable(
                key=contract.object_key,
                content=content,
                media_type=contract.media_type,
                sha256=contract.sha256,
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                ("object-limit", 1, 1_024, 1_024, 1_800_000_000.0, 8, "object limit"),
                ("object-bytes", 10, 1_024, 5, 1_800_000_000.0, 8, "object-byte"),
                ("database-bytes", 10, 4, 1_024, 1_800_000_000.0, 8, "file exceeds"),
                ("clock", 10, 1_024, 1_024, float("nan"), 8, "metadata"),
                ("schema", 10, 1_024, 1_024, 1_800_000_000.0, 0, "metadata"),
            )
            for name, max_objects, max_database, max_bytes, now, schema, message in cases:
                with self.subTest(name=name):
                    manager = ServiceBackupManager(
                        postgres_dsn="postgresql://database/source",
                        catalog=FakeCatalog(BackupDatabaseSnapshot(schema, contracts)),
                        object_store=store,
                        archive_tool=FakeArchiveTool(),
                        max_database_bytes=max_database,
                        max_objects=max_objects,
                        max_total_object_bytes=max_bytes,
                        object_page_size=1,
                        clock=lambda now=now: now,
                        backup_id_factory=lambda: "bkp_0123456789abcdef0123456789abcdef",
                    )
                    output = root / name
                    with self.assertRaisesRegex(ServiceArchiveError, message):
                        manager.create_backup(output)
                    self.assertFalse(output.exists())

            invalid_id = ServiceBackupManager(
                postgres_dsn="postgresql://database/source",
                catalog=FakeCatalog(BackupDatabaseSnapshot(8, ())),
                object_store=store,
                archive_tool=FakeArchiveTool(),
                max_database_bytes=1_024,
                max_objects=10,
                max_total_object_bytes=1_024,
                object_page_size=1,
                backup_id_factory=lambda: "unsafe",
            )
            with self.assertRaisesRegex(ServiceArchiveError, "identifier"):
                invalid_id.create_backup(root / "invalid-id")

    def test_backup_refuses_existing_destination_and_corrupt_published_object(self) -> None:
        content = b"expected"
        contract = _contract("artifacts/tenant/project/batch/audio", content)
        store = MemoryObjectStore()
        store.items[contract.object_key] = (
            b"changed",
            contract.media_type,
            contract.sha256,
        )
        manager = _manager(
            FakeCatalog(BackupDatabaseSnapshot(8, (contract,))),
            store,
            FakeArchiveTool(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "backup"
            output.mkdir()
            with self.assertRaisesRegex(ServiceArchiveError, "already exists"):
                manager.create_backup(output)
            output.rmdir()
            with self.assertRaisesRegex(ServiceArchiveError, "preflight"):
                manager.create_backup(output)
            self.assertFalse(output.exists())

    def test_local_postgres_tool_keeps_dsn_out_of_command_arguments(self) -> None:
        dsn = "postgresql://archive_user:archive_password@database/melqivra"
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "database.dump"

            def run(arguments: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                self.assertNotIn(dsn, " ".join(arguments))
                self.assertEqual("database", kwargs["env"]["PGHOST"])
                self.assertEqual("melqivra", kwargs["env"]["PGDATABASE"])
                self.assertEqual("archive_user", kwargs["env"]["PGUSER"])
                self.assertEqual("archive_password", kwargs["env"]["PGPASSWORD"])
                destination.write_bytes(b"PGDMP")
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch("agent_music.service_backup.shutil.which", return_value="pg_dump"),
                patch("agent_music.service_backup.subprocess.run", side_effect=run),
            ):
                LocalPostgresArchiveTool(timeout_seconds=1).dump(dsn, destination)

        self.assertTrue(destination.exists() is False)

    def test_local_restore_selects_only_database_name_and_sanitizes_failures(self) -> None:
        dsn = "postgresql://archive_user:archive_password@database/restored"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "database.dump"
            source.write_bytes(b"PGDMP")

            def run(arguments: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                self.assertIn("--dbname=restored", arguments)
                self.assertNotIn("archive_password", " ".join(arguments))
                self.assertEqual("archive_password", kwargs["env"]["PGPASSWORD"])
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch("agent_music.service_backup.shutil.which", return_value="pg_restore"),
                patch("agent_music.service_backup.subprocess.run", side_effect=run),
            ):
                LocalPostgresArchiveTool(timeout_seconds=1).restore(dsn, source)

            with (
                patch("agent_music.service_backup.shutil.which", return_value="pg_restore"),
                patch(
                    "agent_music.service_backup.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 1, "", "provider-secret"),
                ),
                self.assertRaisesRegex(ServiceArchiveError, "restore command failed") as raised,
            ):
                LocalPostgresArchiveTool(timeout_seconds=1).restore(dsn, source)
            self.assertNotIn("provider-secret", str(raised.exception))

        with patch("agent_music.service_backup.shutil.which", return_value=None):
            with self.assertRaisesRegex(ServiceArchiveError, "executable is unavailable"):
                LocalPostgresArchiveTool(timeout_seconds=1).restore(dsn, Path("missing"))

    def test_docker_postgres_tool_uses_digest_pin_and_ephemeral_env_file(self) -> None:
        dsn = "postgresql://archive_user:archive_password@127.0.0.1/melqivra"
        image = "postgres:17@sha256:" + "a" * 64
        observed_env_path: Path | None = None
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "database.dump"

            def run(arguments: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                nonlocal observed_env_path
                _ = kwargs
                self.assertNotIn(dsn, " ".join(arguments))
                self.assertIn(image, arguments)
                observed_env_path = Path(arguments[arguments.index("--env-file") + 1])
                environment = observed_env_path.read_text()
                self.assertIn("PGHOST=127.0.0.1\n", environment)
                self.assertIn("PGDATABASE=melqivra\n", environment)
                self.assertIn("PGUSER=archive_user\n", environment)
                self.assertIn("PGPASSWORD=archive_password\n", environment)
                destination.write_bytes(b"PGDMP")
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch("agent_music.service_backup.shutil.which", return_value="docker"),
                patch("agent_music.service_backup.subprocess.run", side_effect=run),
            ):
                DockerPostgresArchiveTool(
                    docker_executable="docker",
                    image=image,
                    timeout_seconds=1,
                ).dump(dsn, destination)

        assert observed_env_path is not None
        self.assertFalse(observed_env_path.exists())

    def test_docker_restore_is_read_only_and_failed_container_is_cleaned(self) -> None:
        dsn = "postgresql://archive_user:archive_password@127.0.0.1/restored"
        image = "postgres:17@sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "database.dump"
            source.write_bytes(b"PGDMP")
            calls: list[tuple[str, ...]] = []

            def run(arguments: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
                _ = kwargs
                rendered = tuple(str(item) for item in arguments)
                calls.append(rendered)
                if "run" in rendered:
                    self.assertIn("--dbname=restored", rendered)
                    self.assertIn("readonly", " ".join(rendered))
                    return subprocess.CompletedProcess(arguments, 1, "", "provider-secret")
                self.assertIn("rm", rendered)
                self.assertIn("--force", rendered)
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch("agent_music.service_backup.shutil.which", return_value="docker"),
                patch("agent_music.service_backup.subprocess.run", side_effect=run),
                self.assertRaisesRegex(ServiceArchiveError, "restore command failed") as raised,
            ):
                DockerPostgresArchiveTool(
                    docker_executable="docker",
                    image=image,
                    timeout_seconds=1,
                ).restore(dsn, source)

        self.assertEqual(2, len(calls))
        self.assertNotIn("provider-secret", str(raised.exception))

    def test_archive_tool_and_manager_configuration_rejects_unsafe_values(self) -> None:
        image = "postgres:17@sha256:" + "a" * 64
        invalid_constructors = (
            lambda: LocalPostgresArchiveTool(dump_executable=""),
            lambda: LocalPostgresArchiveTool(timeout_seconds=0),
            lambda: DockerPostgresArchiveTool(docker_executable="", image=image, timeout_seconds=1),
            lambda: DockerPostgresArchiveTool(
                docker_executable="docker", image="postgres:17", timeout_seconds=1
            ),
            lambda: DockerPostgresArchiveTool(
                docker_executable="docker", image=image, timeout_seconds=0
            ),
            lambda: ServiceBackupManager(
                postgres_dsn="",
                catalog=FakeCatalog(BackupDatabaseSnapshot(8, ())),
                object_store=MemoryObjectStore(),
                archive_tool=FakeArchiveTool(),
                max_database_bytes=1,
                max_objects=1,
                max_total_object_bytes=1,
                object_page_size=1,
            ),
            lambda: ServiceBackupManager(
                postgres_dsn="postgresql://database/source",
                catalog=FakeCatalog(BackupDatabaseSnapshot(8, ())),
                object_store=MemoryObjectStore(),
                archive_tool=FakeArchiveTool(),
                max_database_bytes=1,
                max_objects=1,
                max_total_object_bytes=1,
                object_page_size=1_001,
            ),
        )
        for constructor in invalid_constructors:
            with self.subTest(constructor=constructor), self.assertRaises(ValueError):
                constructor()


if __name__ == "__main__":
    unittest.main()
