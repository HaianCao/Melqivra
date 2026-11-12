"""S03 planner and typed tool boundary tests."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from pathlib import Path

from agent_music.agent_harness import (
    AgentHarness,
    Message,
    OpenAICompatibleClient,
    PlannerError,
    PlannerReply,
    Role,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolPolicyError,
    ToolRegistry,
    untrusted_project_context,
)
from agent_music.domain import ProjectId, UserId


class _Planner:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: object, tools: object) -> PlannerReply:
        self.calls += 1
        if self.calls == 1:
            return PlannerReply("", (ToolCall("call_one", "lyrics.edit", {"text": "new"}),))
        return PlannerReply("done")


class AgentHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolContext(UserId("usr_alice"), ProjectId("prj_song"), "agent")
        self.registry = ToolRegistry()
        self.invocations = 0

        def handler(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
            self.invocations += 1
            return {"revision_id": "rev_new", "text": str(arguments["text"])}

        self.registry.register(
            ToolDefinition(
                "lyrics.edit",
                "Edit lyrics",
                {"type": "object"},
                handler,
                lambda context, arguments: context.user_id == UserId("usr_alice"),
            )
        )

    def test_agent_uses_allowlisted_tool_and_audits_without_content(self) -> None:
        result = AgentHarness(_Planner(), self.registry).run(
            self.context, [Message(Role.USER, "rewrite")]
        )
        self.assertEqual("done", result.text)
        self.assertEqual(1, self.invocations)
        self.assertEqual("success", self.registry.audit_events[0].outcome)
        self.assertFalse(hasattr(self.registry.audit_events[0], "arguments"))

    def test_duplicate_call_is_idempotent_and_unknown_is_rejected(self) -> None:
        call = ToolCall("same", "lyrics.edit", {"text": "one"})
        self.assertEqual(
            self.registry.invoke(self.context, call), self.registry.invoke(self.context, call)
        )
        self.assertEqual(1, self.invocations)
        with self.assertRaisesRegex(ToolPolicyError, "reused"):
            self.registry.invoke(
                self.context,
                ToolCall("same", "lyrics.edit", {"text": "different"}),
            )
        with self.assertRaises(ToolPolicyError):
            self.registry.invoke(self.context, ToolCall("bad", "shell", {}))
        self.assertEqual("not_allowlisted", self.registry.audit_events[-1].outcome)

    def test_approval_cannot_be_bypassed(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "music.generate",
                "Generate",
                {"type": "object"},
                lambda context, arguments: {"job": "job_one"},
                lambda context, arguments: True,
                approval_action="gpu.generate",
            )
        )
        with self.assertRaises(ToolPolicyError):
            registry.invoke(self.context, ToolCall("gpu", "music.generate", {}))

    def test_untrusted_context_marks_injection_as_data(self) -> None:
        corpus = json.loads(
            (Path(__file__).parent / "fixtures" / "s03_injection_corpus.json").read_text(
                encoding="utf-8"
            )
        )
        for attack in corpus:
            with self.subTest(attack=attack):
                message = untrusted_project_context(str(attack))
                self.assertIn("untrusted project data", message.content)
                self.assertIn("<project-data>", message.content)
                self.assertEqual(Role.SYSTEM, message.role)

    def test_openai_client_decodes_tools_and_redacts_http_error(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "lyrics.edit", "arguments": '{"text":"x"}'},
                            }
                        ],
                    },
                }
            ]
        }

        class Reply:
            def read(self) -> bytes:
                return json.dumps(response).encode()

        client = OpenAICompatibleClient(
            base_url="https://example.test/v1",
            api_key="secret",
            model="test",
            opener=lambda request, timeout: Reply(),
        )
        self.assertEqual("lyrics.edit", client.complete([], []).tool_calls[0].name)
        error = urllib.error.HTTPError("https://example.test", 401, "bad secret", {}, io.BytesIO())
        failing = OpenAICompatibleClient(
            base_url="https://example.test/v1",
            api_key="secret",
            model="test",
            opener=lambda request, timeout: (_ for _ in ()).throw(error),
        )
        with self.assertRaisesRegex(PlannerError, "401") as raised:
            failing.complete([], [])
        self.assertNotIn("secret", str(raised.exception))

    def test_client_preserves_tool_message_metadata_and_retries_timeout(self) -> None:
        captured: list[dict[str, object]] = []

        class Reply:
            def read(self) -> bytes:
                return b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'

        def opener(request: object, timeout: float) -> Reply:
            captured.append(json.loads(request.data))  # type: ignore[attr-defined]
            return Reply()

        client = OpenAICompatibleClient(
            base_url="https://provider-two.test/v1",
            api_key="secret",
            model="model-two",
            opener=opener,
        )
        reply = client.complete(
            [
                Message(
                    Role.ASSISTANT,
                    "",
                    tool_calls=(ToolCall("call_one", "lyrics.edit", {"text": "x"}),),
                ),
                Message(Role.TOOL, "{}", "lyrics.edit", "call_one"),
            ],
            [],
        )
        self.assertEqual("ok", reply.text)
        assistant = captured[0]["messages"][0]  # type: ignore[index]
        self.assertEqual("call_one", assistant["tool_calls"][0]["id"])  # type: ignore[index]
        message = captured[0]["messages"][1]  # type: ignore[index]
        self.assertEqual("call_one", message["tool_call_id"])  # type: ignore[index]

        attempts = 0

        def timeout_opener(request: object, timeout: float) -> Reply:
            nonlocal attempts
            attempts += 1
            raise urllib.error.URLError("private prompt content")

        retrying = OpenAICompatibleClient(
            base_url="https://provider-two.test/v1",
            api_key="secret",
            model="model-two",
            max_attempts=2,
            opener=timeout_opener,
        )
        with self.assertRaisesRegex(PlannerError, "transport") as raised:
            retrying.complete([], [])
        self.assertEqual(2, attempts)
        self.assertNotIn("private prompt", str(raised.exception))

    def test_client_rejects_non_object_tool_arguments(self) -> None:
        raw = (
            b'{"choices":[{"message":{"tool_calls":[{"id":"c","function":'
            b'{"name":"lyrics.edit","arguments":"[]"}}]},"finish_reason":"tool_calls"}]}'
        )
        with self.assertRaisesRegex(PlannerError, "invalid"):
            OpenAICompatibleClient._decode(raw)


if __name__ == "__main__":
    unittest.main()
