# S03 evidence — Safe agent harness and tool protocol

Date: 2026-11-12

Status: `DONE`

## Current checkpoint

- Provider-neutral messages, tool calls, and planner reply contracts.
- OpenAI-compatible HTTP client with HTTPS-only configuration, bounded timeout
  and retries, structured response validation, and credential-redacted errors.
- Allowlisted tool registry with JSON Schema publication, tenant-aware
  authorization, per-user call idempotency, explicit approval gates, and minimal
  content-free audit events.
- Bounded agent loop with turn, tool-call, and wall-time limits.
- Explicit untrusted-project-data framing for prompt-injection resistance.
- S02-backed read/edit tools for project, revision, brief, lyrics, blueprint,
  generation plan, takes, and jobs. Mutations always supply actor, reason, and
  expected parent to the immutable repository.
- Explicit approval objects for GPU, external, identity, and destructive risk;
  accepted-revision and restore tools refuse execution without the matching
  approved action.
- Deterministic, priority-ordered context construction with token budget and
  source provenance.
- Streaming text, proposal, diff, approval, result, warning, and error event
  contracts.
- Turn, tool, context-token, wall-time, repeated-failure, cancellation, and
  idempotency-conflict limits.
- Audit linkage from user turn to tool call and resulting revision without
  storing creative content.

Merged implementation checkpoint:
`4f396f3 feat(s03): complete safe agent harness (#14)`.

PR #14 passed all required protected CI jobs before merge across Ubuntu and
Windows on Python 3.11 and 3.13. The earlier provider-client checkpoint also
passed protected CI in PR #13.

The complete local repository gate passed with 86 tests, strict mypy on 25
source files, Ruff format/lint, 81% scheduler/store critical-path coverage,
successful wheel/sdist builds, and no known audited vulnerabilities.

Tests cover two OpenAI-compatible endpoint behaviors, tool-call round trips,
invalid responses, timeout retry, content/key redaction, duplicate replay and
idempotency conflicts, unauthorized and unknown tools, cross-tenant reads,
approval bypass, token/turn/tool/time/repeated-failure limits, cancellation
before mutation, deterministic context replay, the injection corpus, invalid
mutation rollback, and all streaming event classifications.

The stage demo is proven by
`test_reject_approve_and_restore_demo_preserves_history`: a chorus rewrite
creates a proposal without moving the accepted pointer, explicit approval
accepts v2, later work remains separate, and restore creates a new descendant
revision while preserving both the accepted revision and full history.

## Real provider smoke test

The tracked `scripts/s03_openrouter_smoke.py` read `OPEN_ROUTER_TOKEN` from the
ignored local `.env` and made one minimal synthetic request. It emitted only
redacted operational metadata:

```json
{"elapsed_seconds":1.781,"model":"openai/gpt-4o-mini","provider":"openrouter","response_characters":3,"status":"ok","tool_call_count":0}
```

No credential, response text, private project content, or user lyrics were
printed or persisted.

## Deliberate boundaries

- Approval state is an in-process policy object for this stage; production
  durable approvals belong to the S07 service boundary.
- Take and job tools consume injected typed readers. Their production storage
  adapters remain owned by S05/S07 and are not exposed to the planner.
- Creative planning behavior is intentionally deferred to S04; S03 supplies the
  secure capability boundary only.
