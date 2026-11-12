"""Installed remote music worker selected by the allowlisted transport worker."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_music.backends.heartmula import HeartLibEngine, HeartMuLaBackend
from agent_music.domain import JsonObject, JsonValue
from agent_music.generation import (
    MusicBackendRegistry,
    execute_generation,
    request_from_json,
    result_to_json,
)


def generate(
    job: dict[str, Any],
    *,
    registry: MusicBackendRegistry | None = None,
) -> JsonObject:
    """Execute one validated music job with an explicitly configured backend."""

    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise TypeError("music.generate payload must be an object")
    generation_payload = payload.get("generation_request")
    if not isinstance(generation_payload, dict):
        raise TypeError("music.generate requires generation_request")
    output_root = job.get("output_root")
    if not isinstance(output_root, str) or not output_root:
        raise TypeError("music.generate requires output_root")
    request = request_from_json(generation_payload)
    resolved_registry = registry or _configured_registry(Path(output_root))
    backend = resolved_registry.resolve(request.backend_id, request.backend_revision)
    events: list[JsonObject] = []
    result = execute_generation(backend, request, events.append)
    encoded = result_to_json(result)
    event_values: list[JsonValue] = []
    event_values.extend(events)
    encoded["events"] = event_values
    return encoded


def _configured_registry(output_root: Path) -> MusicBackendRegistry:
    selected = os.getenv("MUSIC_BACKEND", "").strip()
    if selected != "heartmula_3b":
        raise RuntimeError("MUSIC_BACKEND must explicitly select heartmula_3b")
    model_root_value = os.getenv("HEARTMULA_MODEL_ROOT", "").strip()
    if not model_root_value:
        raise RuntimeError("HEARTMULA_MODEL_ROOT is required")
    registry = MusicBackendRegistry()
    registry.register(HeartMuLaBackend(HeartLibEngine(Path(model_root_value)), output_root))
    return registry
