"""S03 deterministic context, events, limits, cancellation, and replay tests."""

from __future__ import annotations

import unittest
from collections.abc import Sequence

from agent_music.agent_harness import (
    AgentCancelledError,
    AgentHarness,
    ContextSource,
    EventKind,
    HarnessLimits,
    Message,
    PlannerError,
    PlannerReply,
    Role,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    build_project_context,
)
from agent_music.approval_repository import (
    ApprovalDecision,
    ApprovalRequest,
    InMemoryApprovalRepository,
    RiskClass,
)
from agent_music.domain import JsonObject, ProjectId, UserId


class _Replies:
    def __init__(self, replies: Sequence[PlannerReply]) -> None:
        self.replies = list(replies)

    def complete(self, messages: Sequence[Message], tools: Sequence[JsonObject]) -> PlannerReply:
        return self.replies.pop(0)


class AgentRuntimeTests(unittest.TestCase):
    def test_context_is_deterministic_budgeted_and_provenanced(self) -> None:
        sources = (
            ContextSource("z", "ignored because low priority and large" * 20, 0),
            ContextSource("b", "brief", 2),
            ContextSource("a", "lyrics", 2),
        )
        first = build_project_context(sources, max_tokens=30)
        second = build_project_context(tuple(reversed(sources)), max_tokens=30)
        self.assertEqual(first, second)
        self.assertEqual(("a", "b"), first.source_ids)
        self.assertIn("untrusted project data", first.message.content)

    def test_stream_emits_proposal_diff_and_text(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "lyrics.edit",
                "edit",
                {"type": "object"},
                lambda context, arguments: {"revision_id": "rev_new"},
                lambda context, arguments: True,
            )
        )
        planner = _Replies(
            (
                PlannerReply("", (ToolCall("c1", "lyrics.edit", {}),)),
                PlannerReply("done"),
            )
        )
        events = tuple(
            AgentHarness(planner, registry).run_stream(
                ToolContext(UserId("usr_one"), ProjectId("prj_one"), "agent"),
                [Message(Role.USER, "edit")],
            )
        )
        self.assertEqual(
            (EventKind.PROPOSAL, EventKind.DIFF, EventKind.TEXT),
            tuple(event.kind for event in events),
        )

    def test_stream_classifies_approval_warning_and_repeated_error(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "needs.approval",
                "approval",
                {"type": "object"},
                lambda context, arguments: {},
                lambda context, arguments: True,
                approval_action="external.write",
            )
        )
        approval_events = tuple(
            AgentHarness(
                _Replies(
                    (
                        PlannerReply("", (ToolCall("a", "needs.approval", {}),)),
                        PlannerReply("stop"),
                    )
                ),
                registry,
            ).run_stream(
                ToolContext(UserId("usr_one"), ProjectId("prj_one"), "agent"),
                [Message(Role.USER, "go")],
            )
        )
        self.assertIn(EventKind.APPROVAL, tuple(event.kind for event in approval_events))

        failure_events = []
        stream = AgentHarness(
            _Replies(
                (
                    PlannerReply("", (ToolCall("x", "missing", {}),)),
                    PlannerReply("", (ToolCall("y", "missing", {}),)),
                )
            ),
            ToolRegistry(),
        ).run_stream(
            ToolContext(UserId("usr_one"), ProjectId("prj_one"), "agent"),
            [Message(Role.USER, "go")],
        )
        with self.assertRaises(PlannerError):
            while True:
                failure_events.append(next(stream))
        self.assertIn(EventKind.WARNING, tuple(event.kind for event in failure_events))
        self.assertIn(EventKind.ERROR, tuple(event.kind for event in failure_events))

    def test_cancel_prevents_tool_side_effect(self) -> None:
        invoked = False

        def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            nonlocal invoked
            invoked = True
            return {}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition("safe", "safe", {"type": "object"}, handler, lambda c, a: True)
        )
        planner = _Replies((PlannerReply("", (ToolCall("c", "safe", {}),)),))
        checks = 0

        def cancelled_after_planner() -> bool:
            nonlocal checks
            checks += 1
            return checks > 1

        with self.assertRaises(AgentCancelledError):
            AgentHarness(planner, registry).run(
                ToolContext(UserId("usr_one"), ProjectId("prj_one"), "agent"),
                [Message(Role.USER, "go")],
                is_cancelled=cancelled_after_planner,
            )
        self.assertFalse(invoked)

    def test_repeated_failure_and_token_limits_are_bounded(self) -> None:
        planner = _Replies(
            (
                PlannerReply("", (ToolCall("a", "missing", {}),)),
                PlannerReply("", (ToolCall("b", "missing", {}),)),
            )
        )
        harness = AgentHarness(planner, ToolRegistry())
        with self.assertRaisesRegex(PlannerError, "repeated"):
            harness.run(
                ToolContext(UserId("usr_one"), ProjectId("prj_one"), "agent"),
                [Message(Role.USER, "go")],
            )
        tiny = AgentHarness(
            _Replies((PlannerReply("unused"),)),
            ToolRegistry(),
            HarnessLimits(max_context_tokens=1),
        )
        with self.assertRaisesRegex(PlannerError, "token"):
            tiny.run(
                ToolContext(UserId("usr_one"), ProjectId("prj_one"), "agent"),
                [Message(Role.USER, "too long")],
            )

    def test_approval_repository_is_scoped_and_one_way(self) -> None:
        repository = InMemoryApprovalRepository()
        request = repository.propose(
            ApprovalRequest(
                "approval_one",
                "gpu.generate",
                RiskClass.GPU,
                ProjectId("prj_one"),
                UserId("usr_one"),
            )
        )
        self.assertEqual(ApprovalDecision.PENDING, request.decision)
        repository.decide(
            approval_id="approval_one",
            user_id=UserId("usr_one"),
            approved=True,
        )
        self.assertEqual(
            frozenset({"gpu.generate"}),
            repository.approved_actions(
                user_id=UserId("usr_one"),
                project_id=ProjectId("prj_one"),
            ),
        )
        with self.assertRaises(ValueError):
            repository.decide(
                approval_id="approval_one",
                user_id=UserId("usr_one"),
                approved=False,
            )


if __name__ == "__main__":
    unittest.main()
