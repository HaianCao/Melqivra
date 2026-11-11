"""Run a redacted real-provider smoke test for the S03 planner boundary."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_music.agent_harness import Message, OpenAICompatibleClient, Role


def _read_token(path: Path) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("OPEN_ROUTER_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
            if token:
                return token
    raise RuntimeError("OPEN_ROUTER_TOKEN is not configured")


def main() -> None:
    token = _read_token(Path(".env"))
    model = "openai/gpt-4o-mini"
    client = OpenAICompatibleClient(
        base_url="https://openrouter.ai/api/v1",
        api_key=token,
        model=model,
        timeout_seconds=30.0,
        max_attempts=2,
    )
    started = time.monotonic()
    reply = client.complete(
        [Message(Role.USER, "Reply with exactly OK.")],
        [],
    )
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {
                "provider": "openrouter",
                "model": model,
                "status": "ok" if reply.text.strip() else "empty",
                "elapsed_seconds": round(elapsed, 3),
                "response_characters": len(reply.text),
                "tool_call_count": len(reply.tool_calls),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
