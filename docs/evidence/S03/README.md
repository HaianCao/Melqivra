# S03 evidence — Safe agent harness and tool protocol

Date: 2026-11-12

Status: `IN_PROGRESS`

## Current checkpoint

- Provider-neutral messages, tool calls, and planner reply contracts.
- OpenAI-compatible HTTP client with HTTPS-only configuration, bounded timeout
  and retries, structured response validation, and credential-redacted errors.
- Allowlisted tool registry with JSON Schema publication, tenant-aware
  authorization, per-user call idempotency, explicit approval gates, and minimal
  content-free audit events.
- Bounded agent loop with turn, tool-call, and wall-time limits.
- Explicit untrusted-project-data framing for prompt-injection resistance.

Focused deterministic tests cover successful tool execution, duplicate replay,
unknown tools, approval bypass, injection framing, OpenAI-compatible tool-call
decoding, and error redaction.

## Real provider smoke test

The tracked `scripts/s03_openrouter_smoke.py` read `OPEN_ROUTER_TOKEN` from the
ignored local `.env` and made one minimal synthetic request. It emitted only
redacted operational metadata:

```json
{"elapsed_seconds":1.781,"model":"openai/gpt-4o-mini","provider":"openrouter","response_characters":3,"status":"ok","tool_call_count":0}
```

No credential, response text, private project content, or user lyrics were
printed or persisted.

## Remaining before S03 closure

- Bind concrete project/revision/brief/lyrics/blueprint/take/job tools to the S02
  services with mutation actor/reason/parent enforcement.
- Add deterministic token-budget context construction and streaming event
  contracts.
- Add cancellation/disconnect, repeated-failure, two-endpoint, replay, and
  end-to-end reject/approve/restore evidence.
