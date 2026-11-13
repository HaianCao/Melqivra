"""Named PostgreSQL advisory locks shared by service operations."""

from __future__ import annotations

from agent_music.postgres_migrations import PostgresConnection

# Stable signed 32-bit keys. Changing them would silently break coordination
# between mixed-version API, reconciliation, and backup processes.
_ARTIFACT_LIFECYCLE_LOCK_CLASS = 1_296_387_697
_ARTIFACT_LIFECYCLE_LOCK_OBJECT = 1_812_589_426


def acquire_artifact_lifecycle_shared_lock(connection: PostgresConnection) -> None:
    """Block backup snapshots while an artifact state transaction is active."""

    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(%s, %s)",
        (_ARTIFACT_LIFECYCLE_LOCK_CLASS, _ARTIFACT_LIFECYCLE_LOCK_OBJECT),
    )


def acquire_artifact_lifecycle_exclusive_lock(connection: PostgresConnection) -> None:
    """Hold artifact publication and retention stable for one backup session."""

    connection.execute(
        "SELECT pg_advisory_lock(%s, %s)",
        (_ARTIFACT_LIFECYCLE_LOCK_CLASS, _ARTIFACT_LIFECYCLE_LOCK_OBJECT),
    )


def release_artifact_lifecycle_exclusive_lock(connection: PostgresConnection) -> None:
    """Release the session-level backup lock."""

    connection.execute(
        "SELECT pg_advisory_unlock(%s, %s)",
        (_ARTIFACT_LIFECYCLE_LOCK_CLASS, _ARTIFACT_LIFECYCLE_LOCK_OBJECT),
    )
