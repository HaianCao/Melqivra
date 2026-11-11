# ADR-0001: Domain tools are the agent authority

- Status: Accepted
- Date: 2026-11-11
- Owners: maintainers
- Supersedes: none

## Context

The product uses an LLM planner to collaborate through chat. Giving a planner a
shell, arbitrary filesystem access, direct SQL, or infrastructure credentials
would make authorization, validation, audit, and recovery unreliable.

## Decision

The planner may call only registered, typed domain tools. Each tool validates
input, authorizes the actor, defines idempotency and approval behavior, invokes a
domain service, and emits an audit event. Infrastructure APIs remain private
implementation details. Project documents and tool results are untrusted data,
not planner instructions.

## Alternatives considered

- Raw shell/filesystem tools: flexible but too broad and difficult to secure.
- Let the planner write database records: bypasses domain invariants.
- Hard-coded conversational workflows only: safer but prevents useful agentic
  composition and provider portability.

## Consequences

New capabilities require explicit schemas and policy. This adds implementation
work but produces a stable security and testing boundary. Planner replacement
does not require rewriting domain behavior.

## Validation

Tool contract, authorization, approval-bypass, injection, replay, and audit tests
are mandatory in S03. No planner-facing raw infrastructure tool may exist.

