"""Restart-safe PostgreSQL and immutable-object backup/restore operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from agent_music.artifact_repository import validate_artifact_object_key
from agent_music.object_store import ObjectInfo, ObjectIntegrityError, ObjectStore
from agent_music.postgres_locks import (
    acquire_artifact_lifecycle_exclusive_lock,
    release_artifact_lifecycle_exclusive_lock,
)
from agent_music.postgres_migrations import PostgresConnection

_ARCHIVE_SCHEMA_VERSION = 1
_DATABASE_ARCHIVE_PATH = "database.dump"
_MANIFEST_PATH = "manifest.json"
_MANIFEST_CHECKSUM_PATH = "manifest.sha256"
_OBJECT_PREFIX = "artifacts/"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_ID = re.compile(r"^bkp_[0-9a-f]{32}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_LIBPQ_ENVIRONMENT = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "client_encoding": "PGCLIENTENCODING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "dbname": "PGDATABASE",
    "gssencmode": "PGGSSENCMODE",
    "gsslib": "PGGSSLIB",
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "keepalives": "PGKEEPALIVES",
    "keepalives_count": "PGKEEPALIVESCOUNT",
    "keepalives_idle": "PGKEEPALIVESIDLE",
    "keepalives_interval": "PGKEEPALIVESINTERVAL",
    "krbsrvname": "PGKRBSRVNAME",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "options": "PGOPTIONS",
    "passfile": "PGPASSFILE",
    "password": "PGPASSWORD",
    "port": "PGPORT",
    "replication": "PGREPLICATION",
    "requirepeer": "PGREQUIREPEER",
    "requiressl": "PGREQUIRESSL",
    "service": "PGSERVICE",
    "servicefile": "PGSERVICEFILE",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "sslcert": "PGSSLCERT",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslkey": "PGSSLKEY",
    "sslmode": "PGSSLMODE",
    "sslnegotiation": "PGSSLNEGOTIATION",
    "sslpassword": "PGSSLPASSWORD",
    "sslrootcert": "PGSSLROOTCERT",
    "sslsni": "PGSSLSNI",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "tcp_user_timeout": "PGTCP_USER_TIMEOUT",
    "user": "PGUSER",
}


class ServiceArchiveError(RuntimeError):
    """A backup or restore failed without exposing infrastructure details."""


@dataclass(frozen=True, slots=True)
class BackupObjectContract:
    """One database-authorized object considered by a backup snapshot."""

    object_key: str
    media_type: str
    size_bytes: int
    sha256: str
    state: str


@dataclass(frozen=True, slots=True)
class BackupDatabaseSnapshot:
    """Artifact metadata held stable while PostgreSQL is dumped."""

    schema_version: int
    objects: tuple[BackupObjectContract, ...]


@dataclass(frozen=True, slots=True)
class BackupObjectEntry:
    """One verified immutable object stored in a service archive."""

    object_key: str
    blob_path: str
    media_type: str
    size_bytes: int
    sha256: str
    database_state: str


@dataclass(frozen=True, slots=True)
class DatabaseArchiveEntry:
    """Integrity metadata for the native PostgreSQL dump."""

    archive_path: str
    format: str
    size_bytes: int
    sha256: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class ServiceBackupManifest:
    """Portable, checksummed service-backup manifest."""

    schema_version: int
    backup_id: str
    created_at: float
    database: DatabaseArchiveEntry
    objects: tuple[BackupObjectEntry, ...]
    skipped_incomplete_pending_objects: int


@dataclass(frozen=True, slots=True)
class ServiceBackupReport:
    """Content-free operator result for a completed backup."""

    backup_id: str
    database_schema_version: int
    database_size_bytes: int
    object_count: int
    object_bytes: int
    skipped_incomplete_pending_objects: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ServiceRestoreReport:
    """Content-free operator result for a completed restore."""

    backup_id: str
    database_schema_version: int
    object_count: int
    object_bytes: int
    manifest_sha256: str


class PostgresArchiveTool(Protocol):
    """Native PostgreSQL custom-archive transport."""

    def dump(self, dsn: str, destination: Path) -> None: ...

    def restore(self, dsn: str, source: Path) -> None: ...


class ServiceBackupCatalog(Protocol):
    """PostgreSQL metadata operations required by service archives."""

    def snapshot(self) -> AbstractContextManager[BackupDatabaseSnapshot]: ...

    def require_empty_restore_target(self) -> None: ...

    def verify_restored_manifest(self, manifest: ServiceBackupManifest) -> None: ...


class LocalPostgresArchiveTool:
    """Run installed pg_dump and pg_restore without placing a DSN in argv."""

    def __init__(
        self,
        *,
        dump_executable: str = "pg_dump",
        restore_executable: str = "pg_restore",
        timeout_seconds: float = 3_600.0,
    ) -> None:
        if not dump_executable or not restore_executable:
            raise ValueError("PostgreSQL archive executables must not be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("PostgreSQL archive timeout must be positive")
        self._dump_executable = dump_executable
        self._restore_executable = restore_executable
        self._timeout_seconds = timeout_seconds

    def dump(self, dsn: str, destination: Path) -> None:
        """Write one compressed PostgreSQL custom archive."""

        if destination.exists():
            raise ServiceArchiveError("Database archive destination already exists")
        executable = shutil.which(self._dump_executable)
        if executable is None:
            raise ServiceArchiveError("PostgreSQL dump executable is unavailable")
        self._run(
            executable,
            dsn,
            (
                "--format=custom",
                "--compress=9",
                "--no-owner",
                "--no-privileges",
                f"--file={destination}",
            ),
            operation="backup",
        )
        if not destination.is_file() or destination.is_symlink():
            raise ServiceArchiveError("PostgreSQL backup did not produce an archive")
        _make_private(destination)

    def restore(self, dsn: str, source: Path) -> None:
        """Restore one custom archive into a preflighted empty database."""

        executable = shutil.which(self._restore_executable)
        if executable is None:
            raise ServiceArchiveError("PostgreSQL restore executable is unavailable")
        self._run(
            executable,
            dsn,
            (
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                str(source),
            ),
            operation="restore",
        )

    def _run(
        self,
        executable: str,
        dsn: str,
        arguments: tuple[str, ...],
        *,
        operation: str,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        environment = _libpq_environment(dsn, operation=operation)
        resolved_arguments = arguments
        if operation == "restore":
            database_name = environment.get("PGDATABASE", "")
            if not database_name:
                raise ServiceArchiveError("PostgreSQL restore database is unspecified")
            resolved_arguments = (f"--dbname={database_name}", *arguments)
        try:
            completed = subprocess.run(
                (executable, *resolved_arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ServiceArchiveError(f"PostgreSQL {operation} command failed") from error
        if completed.returncode != 0:
            raise ServiceArchiveError(f"PostgreSQL {operation} command failed")


class DockerPostgresArchiveTool:
    """Run pinned pg_dump/pg_restore clients in a disposable Docker container."""

    def __init__(
        self,
        *,
        docker_executable: str,
        image: str,
        timeout_seconds: float = 3_600.0,
    ) -> None:
        if not docker_executable:
            raise ValueError("Docker executable must not be empty")
        if "@sha256:" not in image or image.endswith("@sha256:"):
            raise ValueError("PostgreSQL archive image must be digest-pinned")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("PostgreSQL archive timeout must be positive")
        self._docker_executable = docker_executable
        self._image = image
        self._timeout_seconds = timeout_seconds

    def dump(self, dsn: str, destination: Path) -> None:
        """Write one custom archive through a pinned PostgreSQL client image."""

        if destination.exists():
            raise ServiceArchiveError("Database archive destination already exists")
        self._run_container(
            dsn,
            destination,
            command=(
                "pg_dump",
                "--format=custom",
                "--compress=9",
                "--no-owner",
                "--no-privileges",
                f"--file=/archive/{destination.name}",
            ),
            operation="backup",
            read_only=False,
        )
        if not destination.is_file() or destination.is_symlink():
            raise ServiceArchiveError("PostgreSQL backup did not produce an archive")
        _make_private(destination)

    def restore(self, dsn: str, source: Path) -> None:
        """Restore one custom archive through a pinned PostgreSQL client image."""

        self._run_container(
            dsn,
            source,
            command=(
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                f"/archive/{source.name}",
            ),
            operation="restore",
            read_only=True,
        )

    def _run_container(
        self,
        dsn: str,
        archive_path: Path,
        *,
        command: tuple[str, ...],
        operation: str,
        read_only: bool,
    ) -> None:
        executable = shutil.which(self._docker_executable)
        if executable is None:
            raise ServiceArchiveError("Docker executable is unavailable")
        connection_environment = _libpq_environment(dsn, operation=operation)
        resolved_command = command
        if operation == "restore":
            database_name = connection_environment.get("PGDATABASE", "")
            if not database_name:
                raise ServiceArchiveError("PostgreSQL restore database is unspecified")
            resolved_command = (command[0], f"--dbname={database_name}", *command[1:])
        archive_directory = archive_path.parent.resolve()
        if "," in str(archive_directory):
            raise ServiceArchiveError("Archive path is not Docker-mount compatible")
        environment_directory = Path(tempfile.mkdtemp(prefix="melqivra-pg-client-"))
        environment_path = environment_directory / "postgres.env"
        container_name = f"melqivra-pg-client-{uuid.uuid4().hex[:16]}"
        mount = f"type=bind,source={archive_directory},target=/archive"
        if read_only:
            mount += ",readonly"
        arguments = [
            executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "host",
            "--env-file",
            str(environment_path),
            "--mount",
            mount,
        ]
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if os.name != "nt" and callable(getuid) and callable(getgid):
            arguments.extend(("--user", f"{getuid()}:{getgid()}"))
        arguments.extend((self._image, *resolved_command))
        try:
            environment_content = "".join(
                f"{key}={value}\n"
                for key, value in sorted(connection_environment.items())
                if key.startswith("PG")
            )
            _write_private(environment_path, environment_content.encode())
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._remove_container(executable, container_name)
            raise ServiceArchiveError(f"PostgreSQL {operation} command failed") from error
        finally:
            environment_path.unlink(missing_ok=True)
            with suppress(OSError):
                environment_directory.rmdir()
        if completed.returncode != 0:
            self._remove_container(executable, container_name)
            raise ServiceArchiveError(f"PostgreSQL {operation} command failed")

    @staticmethod
    def _remove_container(executable: str, name: str) -> None:
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                (executable, "rm", "--force", name),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )


class PostgresServiceBackupCatalog:
    """PostgreSQL backup inventory and restore preflight authority."""

    def __init__(self, dsn: str, *, connect_timeout_seconds: float = 10.0) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if not math.isfinite(connect_timeout_seconds) or connect_timeout_seconds <= 0:
            raise ValueError("PostgreSQL connection timeout must be positive")
        self._dsn = dsn
        self._connect_timeout_seconds = connect_timeout_seconds

    @contextmanager
    def snapshot(self) -> Iterator[BackupDatabaseSnapshot]:
        """Hold artifact lifecycle stable and expose its backup inventory."""

        try:
            connection = psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=math.ceil(self._connect_timeout_seconds),
                row_factory=dict_row,
            )
        except Exception as error:
            raise ServiceArchiveError("PostgreSQL backup preflight failed") from error
        protocol_connection: PostgresConnection = connection
        try:
            acquire_artifact_lifecycle_exclusive_lock(protocol_connection)
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            if version_row is None:
                raise ServiceArchiveError("PostgreSQL migration catalog is unavailable")
            rows = connection.execute(
                """
                SELECT object_key, media_type, size_bytes, sha256, state
                FROM artifacts
                WHERE state IN ('pending', 'published')
                ORDER BY object_key
                """
            ).fetchall()
            objects = tuple(
                BackupObjectContract(
                    object_key=str(row["object_key"]),
                    media_type=str(row["media_type"]),
                    size_bytes=int(row["size_bytes"]),
                    sha256=str(row["sha256"]),
                    state=str(row["state"]),
                )
                for row in rows
            )
        except Exception as error:
            with suppress(Exception):
                release_artifact_lifecycle_exclusive_lock(protocol_connection)
            connection.close()
            raise ServiceArchiveError("PostgreSQL backup inventory failed") from error
        try:
            yield BackupDatabaseSnapshot(int(version_row["version"]), objects)
        finally:
            with suppress(Exception):
                release_artifact_lifecycle_exclusive_lock(protocol_connection)
            connection.close()

    def require_empty_restore_target(self) -> None:
        """Refuse destructive restore over any existing public database object."""

        try:
            with psycopg.connect(
                self._dsn,
                connect_timeout=math.ceil(self._connect_timeout_seconds),
                row_factory=dict_row,
            ) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS object_count
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relkind IN ('r', 'p', 'S', 'v', 'm')
                    """
                ).fetchone()
        except Exception as error:
            raise ServiceArchiveError("PostgreSQL restore preflight failed") from error
        if row is None or int(row["object_count"]) != 0:
            raise ServiceArchiveError("PostgreSQL restore target is not empty")

    def verify_restored_manifest(self, manifest: ServiceBackupManifest) -> None:
        """Verify restored schema and every published object contract."""

        expected = {
            entry.object_key: entry
            for entry in manifest.objects
            if entry.database_state == "published"
        }
        try:
            with psycopg.connect(
                self._dsn,
                connect_timeout=math.ceil(self._connect_timeout_seconds),
                row_factory=dict_row,
            ) as connection:
                version_row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT object_key, media_type, size_bytes, sha256
                    FROM artifacts
                    WHERE state = 'published'
                    ORDER BY object_key
                    """
                ).fetchall()
        except Exception as error:
            raise ServiceArchiveError("PostgreSQL restore verification failed") from error
        if version_row is None or int(version_row["version"]) != manifest.database.schema_version:
            raise ServiceArchiveError("Restored PostgreSQL schema version does not match backup")
        observed = {
            str(row["object_key"]): (
                str(row["media_type"]),
                int(row["size_bytes"]),
                str(row["sha256"]),
            )
            for row in rows
        }
        expected_contracts = {
            key: (entry.media_type, entry.size_bytes, entry.sha256)
            for key, entry in expected.items()
        }
        if observed != expected_contracts:
            raise ServiceArchiveError("Restored PostgreSQL artifact metadata is incomplete")


class ServiceBackupManager:
    """Create and restore bounded, independently verifiable service archives."""

    def __init__(
        self,
        *,
        postgres_dsn: str,
        catalog: ServiceBackupCatalog,
        object_store: ObjectStore,
        archive_tool: PostgresArchiveTool,
        max_database_bytes: int,
        max_objects: int,
        max_total_object_bytes: int,
        object_page_size: int,
        clock: Callable[[], float] = time.time,
        backup_id_factory: Callable[[], str] = lambda: f"bkp_{uuid.uuid4().hex}",
    ) -> None:
        if not postgres_dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if min(max_database_bytes, max_objects, max_total_object_bytes, object_page_size) < 1:
            raise ValueError("Service archive bounds must be positive")
        if object_page_size > 1_000:
            raise ValueError("Service archive page size must not exceed 1000")
        self._postgres_dsn = postgres_dsn
        self._catalog = catalog
        self._object_store = object_store
        self._archive_tool = archive_tool
        self._max_database_bytes = max_database_bytes
        self._max_objects = max_objects
        self._max_total_object_bytes = max_total_object_bytes
        self._object_page_size = object_page_size
        self._clock = clock
        self._backup_id_factory = backup_id_factory

    def close(self) -> None:
        """Release object-store network resources."""

        self._object_store.close()

    def create_backup(self, output_directory: Path) -> ServiceBackupReport:
        """Atomically create a database dump plus verified immutable object set."""

        output = output_directory.resolve()
        if output.exists() or output.is_symlink():
            raise ServiceArchiveError("Backup destination already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        backup_id = str(self._backup_id_factory())
        if _BACKUP_ID.fullmatch(backup_id) is None:
            raise ServiceArchiveError("Backup identifier is invalid")
        staging = output.parent / f".{output.name}.pending-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            with self._catalog.snapshot() as snapshot:
                if not isinstance(snapshot, BackupDatabaseSnapshot):
                    raise ServiceArchiveError("PostgreSQL backup snapshot is invalid")
                if len(snapshot.objects) > self._max_objects:
                    raise ServiceArchiveError("Service archive object limit exceeded")
                database_path = staging / _DATABASE_ARCHIVE_PATH
                self._archive_tool.dump(self._postgres_dsn, database_path)
                database_size, database_sha256 = _hash_file(
                    database_path,
                    maximum_bytes=self._max_database_bytes,
                )
                entries, object_bytes, skipped_pending = self._backup_objects(
                    staging,
                    snapshot.objects,
                )
                manifest = ServiceBackupManifest(
                    schema_version=_ARCHIVE_SCHEMA_VERSION,
                    backup_id=backup_id,
                    created_at=float(self._clock()),
                    database=DatabaseArchiveEntry(
                        archive_path=_DATABASE_ARCHIVE_PATH,
                        format="postgresql-custom",
                        size_bytes=database_size,
                        sha256=database_sha256,
                        schema_version=snapshot.schema_version,
                    ),
                    objects=entries,
                    skipped_incomplete_pending_objects=skipped_pending,
                )
                if (
                    manifest.database.schema_version < 1
                    or not math.isfinite(manifest.created_at)
                    or manifest.created_at < 0
                ):
                    raise ServiceArchiveError("Service backup metadata is invalid")
                manifest_sha256 = _write_manifest(staging, manifest)
            staging.replace(output)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return ServiceBackupReport(
            backup_id=backup_id,
            database_schema_version=manifest.database.schema_version,
            database_size_bytes=manifest.database.size_bytes,
            object_count=len(manifest.objects),
            object_bytes=object_bytes,
            skipped_incomplete_pending_objects=skipped_pending,
            manifest_sha256=manifest_sha256,
        )

    def restore_backup(self, input_directory: Path) -> ServiceRestoreReport:
        """Restore verified bytes first, then metadata into an empty database."""

        archive_root = input_directory.resolve(strict=True)
        if not archive_root.is_dir() or archive_root.is_symlink():
            raise ServiceArchiveError("Service archive path is invalid")
        manifest, manifest_sha256 = _read_manifest(archive_root, self._max_objects)
        database_path = _archive_file(archive_root, manifest.database.archive_path)
        database_size, database_sha256 = _hash_file(
            database_path,
            maximum_bytes=self._max_database_bytes,
        )
        if (
            database_size != manifest.database.size_bytes
            or database_sha256 != manifest.database.sha256
        ):
            raise ServiceArchiveError("PostgreSQL archive integrity verification failed")
        object_bytes = self._verify_archive_objects(archive_root, manifest.objects)
        self._catalog.require_empty_restore_target()
        self._require_compatible_object_target(manifest.objects)
        for entry in manifest.objects:
            content = _read_file_bounded(
                _archive_file(archive_root, entry.blob_path),
                maximum_bytes=entry.size_bytes,
            )
            self._object_store.put_immutable(
                key=entry.object_key,
                content=content,
                media_type=entry.media_type,
                sha256=entry.sha256,
            )
        self._archive_tool.restore(self._postgres_dsn, database_path)
        self._catalog.verify_restored_manifest(manifest)
        self._verify_restored_objects(manifest.objects)
        return ServiceRestoreReport(
            backup_id=manifest.backup_id,
            database_schema_version=manifest.database.schema_version,
            object_count=len(manifest.objects),
            object_bytes=object_bytes,
            manifest_sha256=manifest_sha256,
        )

    def _backup_objects(
        self,
        staging: Path,
        contracts: tuple[BackupObjectContract, ...],
    ) -> tuple[tuple[BackupObjectEntry, ...], int, int]:
        entries: list[BackupObjectEntry] = []
        total_bytes = 0
        skipped_pending = 0
        observed_keys: set[str] = set()
        for contract in contracts:
            _validate_contract(contract)
            if contract.object_key in observed_keys:
                raise ServiceArchiveError("Backup inventory contains duplicate object keys")
            observed_keys.add(contract.object_key)
            observed = self._object_store.stat(contract.object_key)
            if not _object_matches(observed, contract):
                if contract.state == "pending":
                    skipped_pending += 1
                    continue
                raise ServiceArchiveError("Published object failed backup preflight")
            try:
                content = self._object_store.get(
                    contract.object_key,
                    max_bytes=contract.size_bytes,
                )
            except (KeyError, ObjectIntegrityError) as error:
                if contract.state == "pending":
                    skipped_pending += 1
                    continue
                raise ServiceArchiveError("Published object failed backup verification") from error
            digest = hashlib.sha256(content).hexdigest()
            if len(content) != contract.size_bytes or digest != contract.sha256:
                if contract.state == "pending":
                    skipped_pending += 1
                    continue
                raise ServiceArchiveError("Published object failed backup verification")
            total_bytes += len(content)
            if total_bytes > self._max_total_object_bytes:
                raise ServiceArchiveError("Service archive object-byte limit exceeded")
            blob_path = _blob_path(contract.sha256)
            destination = staging.joinpath(*PurePosixPath(blob_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                size, prior_digest = _hash_file(
                    destination,
                    maximum_bytes=contract.size_bytes,
                )
                if size != contract.size_bytes or prior_digest != contract.sha256:
                    raise ServiceArchiveError("Service archive content-address collision")
            else:
                _write_private(destination, content)
            entries.append(
                BackupObjectEntry(
                    object_key=contract.object_key,
                    blob_path=blob_path,
                    media_type=contract.media_type,
                    size_bytes=contract.size_bytes,
                    sha256=contract.sha256,
                    database_state=contract.state,
                )
            )
        return tuple(entries), total_bytes, skipped_pending

    def _verify_archive_objects(
        self,
        archive_root: Path,
        entries: tuple[BackupObjectEntry, ...],
    ) -> int:
        total_bytes = 0
        observed_keys: set[str] = set()
        for entry in entries:
            _validate_entry(entry)
            if entry.object_key in observed_keys:
                raise ServiceArchiveError("Service archive contains duplicate object keys")
            observed_keys.add(entry.object_key)
            size, digest = _hash_file(
                _archive_file(archive_root, entry.blob_path),
                maximum_bytes=entry.size_bytes,
            )
            if size != entry.size_bytes or digest != entry.sha256:
                raise ServiceArchiveError("Service archive object integrity verification failed")
            total_bytes += size
            if total_bytes > self._max_total_object_bytes:
                raise ServiceArchiveError("Service archive object-byte limit exceeded")
        return total_bytes

    def _require_compatible_object_target(
        self,
        entries: tuple[BackupObjectEntry, ...],
    ) -> None:
        expected = {entry.object_key: entry for entry in entries}
        cursor: str | None = None
        observed_count = 0
        observed_cursors: set[str] = set()
        while True:
            page = self._object_store.list(
                _OBJECT_PREFIX,
                cursor=cursor,
                limit=self._object_page_size,
            )
            for item in page.items:
                observed_count += 1
                if observed_count > self._max_objects:
                    raise ServiceArchiveError("Restore object target exceeds configured limit")
                entry = expected.get(item.key)
                observed = self._object_store.stat(item.key)
                if entry is None or not _entry_matches(observed, entry):
                    raise ServiceArchiveError("Restore object target is not empty or compatible")
            if page.next_cursor is None:
                return
            if page.next_cursor in observed_cursors:
                raise ServiceArchiveError("Restore object listing cursor repeated")
            observed_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    def _verify_restored_objects(self, entries: tuple[BackupObjectEntry, ...]) -> None:
        for entry in entries:
            observed = self._object_store.stat(entry.object_key)
            if not _entry_matches(observed, entry):
                raise ServiceArchiveError("Restored object metadata verification failed")
            try:
                content = self._object_store.get(entry.object_key, max_bytes=entry.size_bytes)
            except (KeyError, ObjectIntegrityError) as error:
                raise ServiceArchiveError("Restored object verification failed") from error
            if (
                len(content) != entry.size_bytes
                or hashlib.sha256(content).hexdigest() != entry.sha256
            ):
                raise ServiceArchiveError("Restored object verification failed")


def _validate_contract(contract: BackupObjectContract) -> None:
    validate_artifact_object_key(contract.object_key)
    if _MEDIA_TYPE.fullmatch(contract.media_type) is None:
        raise ServiceArchiveError("Backup object media type is invalid")
    if contract.size_bytes < 0 or _SHA256.fullmatch(contract.sha256) is None:
        raise ServiceArchiveError("Backup object integrity contract is invalid")
    if contract.state not in {"pending", "published"}:
        raise ServiceArchiveError("Backup object state is invalid")


def _libpq_environment(dsn: str, *, operation: str) -> dict[str, str]:
    if not dsn or "\n" in dsn or "\r" in dsn:
        raise ValueError("PostgreSQL DSN is invalid")
    try:
        connection_parameters = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as error:
        raise ValueError("PostgreSQL DSN is invalid") from error
    unsupported = set(connection_parameters) - set(_LIBPQ_ENVIRONMENT)
    if unsupported:
        raise ServiceArchiveError("PostgreSQL DSN uses unsupported archive options")
    environment = os.environ.copy()
    for key, value in connection_parameters.items():
        if value is None:
            continue
        rendered = str(value)
        if "\n" in rendered or "\r" in rendered:
            raise ValueError("PostgreSQL DSN is invalid")
        environment[_LIBPQ_ENVIRONMENT[key]] = rendered
    environment["PGAPPNAME"] = f"melqivra-service-{operation}"
    return environment


def _validate_entry(entry: BackupObjectEntry) -> None:
    _validate_contract(
        BackupObjectContract(
            object_key=entry.object_key,
            media_type=entry.media_type,
            size_bytes=entry.size_bytes,
            sha256=entry.sha256,
            state=entry.database_state,
        )
    )
    if entry.blob_path != _blob_path(entry.sha256):
        raise ServiceArchiveError("Service archive object path is invalid")


def _object_matches(
    observed: ObjectInfo | None,
    contract: BackupObjectContract,
) -> bool:
    return (
        observed is not None
        and observed.size_bytes == contract.size_bytes
        and observed.sha256 == contract.sha256
        and observed.media_type == contract.media_type
    )


def _entry_matches(observed: ObjectInfo | None, entry: BackupObjectEntry) -> bool:
    return (
        observed is not None
        and observed.size_bytes == entry.size_bytes
        and observed.sha256 == entry.sha256
        and observed.media_type == entry.media_type
    )


def _blob_path(sha256: str) -> str:
    if _SHA256.fullmatch(sha256) is None:
        raise ServiceArchiveError("Service archive object checksum is invalid")
    return f"objects/{sha256[:2]}/{sha256}.bin"


def _write_manifest(root: Path, manifest: ServiceBackupManifest) -> str:
    payload = asdict(manifest)
    payload["objects"] = [asdict(item) for item in manifest.objects]
    canonical = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    digest = hashlib.sha256(canonical).hexdigest()
    _write_private(root / _MANIFEST_PATH, canonical)
    _write_private(
        root / _MANIFEST_CHECKSUM_PATH,
        f"{digest}  {_MANIFEST_PATH}\n".encode("ascii"),
    )
    return digest


def _read_manifest(root: Path, max_objects: int) -> tuple[ServiceBackupManifest, str]:
    checksum_path = _archive_file(root, _MANIFEST_CHECKSUM_PATH)
    checksum_text = checksum_path.read_text(encoding="ascii")
    match = re.fullmatch(r"([0-9a-f]{64})  manifest\.json\n", checksum_text)
    if match is None:
        raise ServiceArchiveError("Service archive manifest checksum file is invalid")
    manifest_path = _archive_file(root, _MANIFEST_PATH)
    content = manifest_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != match.group(1):
        raise ServiceArchiveError("Service archive manifest integrity verification failed")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceArchiveError("Service archive manifest is invalid") from error
    manifest = _parse_manifest(payload, max_objects=max_objects)
    canonical = (
        json.dumps(
            asdict(manifest),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if canonical != content:
        raise ServiceArchiveError("Service archive manifest is not canonical")
    return manifest, digest


def _parse_manifest(value: object, *, max_objects: int) -> ServiceBackupManifest:
    payload = _require_mapping(value, "manifest")
    _require_exact_keys(
        payload,
        {
            "backup_id",
            "created_at",
            "database",
            "objects",
            "schema_version",
            "skipped_incomplete_pending_objects",
        },
        "manifest",
    )
    schema_version = _require_int(payload["schema_version"], "schema_version")
    if schema_version != _ARCHIVE_SCHEMA_VERSION:
        raise ServiceArchiveError("Service archive schema version is unsupported")
    backup_id = _require_string(payload["backup_id"], "backup_id")
    if _BACKUP_ID.fullmatch(backup_id) is None:
        raise ServiceArchiveError("Service archive backup ID is invalid")
    created_at = _require_number(payload["created_at"], "created_at")
    if not math.isfinite(created_at) or created_at < 0:
        raise ServiceArchiveError("Service archive creation time is invalid")
    database = _parse_database_entry(payload["database"])
    raw_objects = payload["objects"]
    if not isinstance(raw_objects, list) or len(raw_objects) > max_objects:
        raise ServiceArchiveError("Service archive object list is invalid")
    objects = tuple(_parse_object_entry(item) for item in raw_objects)
    skipped = _require_int(
        payload["skipped_incomplete_pending_objects"],
        "skipped_incomplete_pending_objects",
    )
    if skipped < 0:
        raise ServiceArchiveError("Service archive pending-object count is invalid")
    return ServiceBackupManifest(
        schema_version=schema_version,
        backup_id=backup_id,
        created_at=created_at,
        database=database,
        objects=objects,
        skipped_incomplete_pending_objects=skipped,
    )


def _parse_database_entry(value: object) -> DatabaseArchiveEntry:
    payload = _require_mapping(value, "database")
    _require_exact_keys(
        payload,
        {"archive_path", "format", "schema_version", "sha256", "size_bytes"},
        "database",
    )
    entry = DatabaseArchiveEntry(
        archive_path=_require_string(payload["archive_path"], "archive_path"),
        format=_require_string(payload["format"], "format"),
        size_bytes=_require_int(payload["size_bytes"], "size_bytes"),
        sha256=_require_string(payload["sha256"], "sha256"),
        schema_version=_require_int(payload["schema_version"], "schema_version"),
    )
    if (
        entry.archive_path != _DATABASE_ARCHIVE_PATH
        or entry.format != "postgresql-custom"
        or entry.size_bytes < 1
        or _SHA256.fullmatch(entry.sha256) is None
        or entry.schema_version < 1
    ):
        raise ServiceArchiveError("Service archive database contract is invalid")
    return entry


def _parse_object_entry(value: object) -> BackupObjectEntry:
    payload = _require_mapping(value, "object")
    _require_exact_keys(
        payload,
        {
            "blob_path",
            "database_state",
            "media_type",
            "object_key",
            "sha256",
            "size_bytes",
        },
        "object",
    )
    entry = BackupObjectEntry(
        object_key=_require_string(payload["object_key"], "object_key"),
        blob_path=_require_string(payload["blob_path"], "blob_path"),
        media_type=_require_string(payload["media_type"], "media_type"),
        size_bytes=_require_int(payload["size_bytes"], "size_bytes"),
        sha256=_require_string(payload["sha256"], "sha256"),
        database_state=_require_string(payload["database_state"], "database_state"),
    )
    _validate_entry(entry)
    return entry


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ServiceArchiveError(f"Service archive {name} must be an object")
    return cast(Mapping[str, object], value)


def _require_exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ServiceArchiveError(f"Service archive {name} fields are invalid")


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ServiceArchiveError(f"Service archive {name} must be a string")
    return value


def _require_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceArchiveError(f"Service archive {name} must be an integer")
    return value


def _require_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ServiceArchiveError(f"Service archive {name} must be a number")
    return float(value)


def _archive_file(root: Path, relative: str) -> Path:
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
        raise ServiceArchiveError("Service archive contains an unsafe path")
    candidate = root.joinpath(*portable.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise ServiceArchiveError("Service archive file is missing or unsafe")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ServiceArchiveError("Service archive path escaped its root") from error
    return resolved


def _hash_file(path: Path, *, maximum_bytes: int) -> tuple[int, str]:
    if maximum_bytes < 1:
        raise ValueError("File hash bound must be positive")
    if path.is_symlink() or not path.is_file():
        raise ServiceArchiveError("Service archive file is missing or unsafe")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1_048_576):
                size += len(chunk)
                if size > maximum_bytes:
                    raise ServiceArchiveError("Service archive file exceeds configured limit")
                digest.update(chunk)
    except OSError as error:
        raise ServiceArchiveError("Service archive file could not be read") from error
    return size, digest.hexdigest()


def _read_file_bounded(path: Path, *, maximum_bytes: int) -> bytes:
    if maximum_bytes < 0:
        raise ValueError("File read bound must not be negative")
    try:
        with path.open("rb") as handle:
            content = handle.read(maximum_bytes + 1)
    except OSError as error:
        raise ServiceArchiveError("Service archive file could not be read") from error
    if len(content) > maximum_bytes:
        raise ServiceArchiveError("Service archive file exceeds configured limit")
    return content


def _write_private(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(content)
        _make_private(path)
    except OSError as error:
        raise ServiceArchiveError("Service archive file could not be written") from error


def _make_private(path: Path) -> None:
    with suppress(OSError):
        path.chmod(0o600)
