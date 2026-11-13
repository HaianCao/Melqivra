"""Durable idempotency and content-minimal audit boundary for domain tools."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from agent_music.domain import JsonObject, ProjectId, UserId


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Content-minimal record of one tool policy or execution outcome."""

    user_id: UserId
    project_id: ProjectId
    actor: str
    tool_name: str
    tool_call_id: str
    outcome: str
    result_revision_id: str | None = None
    turn_id: str = "turn_unknown"


class ToolExecutionDisposition(StrEnum):
    """Result of atomically reserving one tenant-scoped tool call ID."""

    EXECUTE = "execute"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ToolExecutionClaim:
    """Reservation decision and optional prior result for an idempotent replay."""

    disposition: ToolExecutionDisposition
    result: JsonObject | None = None


class ToolExecutionRepository(Protocol):
    """Atomic tool-call reservation, replay, and append-only audit authority."""

    def close(self) -> None: ...

    def begin(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        tool_name: str,
        input_sha256: str,
        actor: str,
        turn_id: str,
    ) -> ToolExecutionClaim: ...

    def complete_success(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        tool_name: str,
        input_sha256: str,
        result: JsonObject,
        event: AuditEvent,
    ) -> None: ...

    def abandon(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        input_sha256: str,
    ) -> None: ...

    def append_audit(self, event: AuditEvent) -> None: ...

    def list_audit_events(
        self,
        *,
        user_id: UserId | None = None,
        project_id: ProjectId | None = None,
    ) -> tuple[AuditEvent, ...]: ...


@dataclass(slots=True)
class _InMemoryExecution:
    tool_name: str
    input_sha256: str
    completed: bool = False
    result: JsonObject | None = None


class InMemoryToolExecutionRepository:
    """Thread-safe development adapter for tool replay and minimal audit."""

    def __init__(self) -> None:
        self._executions: dict[tuple[UserId, ProjectId, str], _InMemoryExecution] = {}
        self._audit: list[AuditEvent] = []
        self._lock = threading.RLock()

    def close(self) -> None:
        """Release adapter resources; the in-memory adapter owns none."""

    def begin(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        tool_name: str,
        input_sha256: str,
        actor: str,
        turn_id: str,
    ) -> ToolExecutionClaim:
        """Reserve a new call or classify an existing tenant-scoped call."""

        del actor, turn_id
        key = (user_id, project_id, tool_call_id)
        with self._lock:
            current = self._executions.get(key)
            if current is None:
                self._executions[key] = _InMemoryExecution(tool_name, input_sha256)
                return ToolExecutionClaim(ToolExecutionDisposition.EXECUTE)
            if current.tool_name != tool_name or current.input_sha256 != input_sha256:
                return ToolExecutionClaim(ToolExecutionDisposition.CONFLICT)
            if not current.completed:
                return ToolExecutionClaim(ToolExecutionDisposition.IN_PROGRESS)
            assert current.result is not None
            return ToolExecutionClaim(
                ToolExecutionDisposition.REPLAY,
                _canonical_result(current.result),
            )

    def complete_success(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        tool_name: str,
        input_sha256: str,
        result: JsonObject,
        event: AuditEvent,
    ) -> None:
        """Atomically publish the replay result and its success audit event."""

        key = (user_id, project_id, tool_call_id)
        with self._lock:
            current = self._executions.get(key)
            if (
                current is None
                or current.completed
                or current.tool_name != tool_name
                or current.input_sha256 != input_sha256
            ):
                raise RuntimeError("Tool execution reservation was lost")
            current.result = _canonical_result(result)
            current.completed = True
            self._audit.append(event)

    def abandon(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        input_sha256: str,
    ) -> None:
        """Release a failed in-process reservation so corrected work may retry."""

        key = (user_id, project_id, tool_call_id)
        with self._lock:
            current = self._executions.get(key)
            if (
                current is not None
                and not current.completed
                and current.input_sha256 == input_sha256
            ):
                del self._executions[key]

    def append_audit(self, event: AuditEvent) -> None:
        """Append one content-minimal policy or failure event."""

        with self._lock:
            self._audit.append(event)

    def list_audit_events(
        self,
        *,
        user_id: UserId | None = None,
        project_id: ProjectId | None = None,
    ) -> tuple[AuditEvent, ...]:
        """List audit events, optionally restricted to one tenant or project."""

        with self._lock:
            return tuple(
                event
                for event in self._audit
                if (user_id is None or event.user_id == user_id)
                and (project_id is None or event.project_id == project_id)
            )


def tool_input_sha256(tool_name: str, arguments: JsonObject) -> str:
    """Hash canonical tool identity and input without persisting the arguments."""

    encoded = json.dumps(
        {"name": tool_name, "arguments": arguments},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_result_json(result: JsonObject) -> str:
    """Serialize a tool result deterministically for durable idempotent replay."""

    return json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_result_json(value: str) -> JsonObject:
    """Decode one persisted tool result and reject non-object corruption."""

    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("Persisted tool result must be an object")
    return cast(JsonObject, decoded)


def _canonical_result(result: JsonObject) -> JsonObject:
    return decode_result_json(canonical_result_json(result))
