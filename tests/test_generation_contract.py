"""S05 backend-neutral generation contract tests."""

from __future__ import annotations

import unittest

from agent_music.domain import ProjectId, RevisionId
from agent_music.generation import (
    GeneratedTake,
    GenerationBatchResult,
    GenerationContractError,
    MusicBackendDescriptor,
    MusicBackendRegistry,
    MusicGenerationRequest,
    TakeSpec,
    UnsupportedGenerationError,
    VocalMode,
    execute_generation,
    request_from_json,
    request_to_json,
    result_from_json,
    result_to_json,
)


class StubBackend:
    def __init__(self, descriptor: MusicBackendDescriptor) -> None:
        self._descriptor = descriptor
        self.calls = 0

    @property
    def descriptor(self) -> MusicBackendDescriptor:
        return self._descriptor

    def generate(self, request, on_progress):  # type: ignore[no-untyped-def]
        self.calls += 1
        on_progress({"type": "stage", "stage": "inference", "progress": 0.5})
        takes = tuple(
            GeneratedTake(
                take.take_id,
                take.seed,
                f"takes/{take.take_id}.wav",
                "audio/wav",
                100,
                f"{take.seed:064x}",
                request.duration_seconds,
                48_000,
                2,
                {"passed": True, "clipped": False, "silent": False},
            )
            for take in request.takes
        )
        return GenerationBatchResult(
            request.backend_id,
            request.backend_revision,
            request.contract_checksum,
            takes,
            {"inference_seconds": 1.0},
        )


class GenerationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptor = MusicBackendDescriptor(
            backend_id="heartmula_3b",
            source_revision="source-pin",
            model_ids=("model@revision", "codec@revision"),
            license_ids=("Apache-2.0",),
            operations=frozenset({"text2music"}),
            supported_languages=frozenset({"en", "vi"}),
            supports_vocals=True,
            supports_instrumental=False,
            min_duration_seconds=20.0,
            max_duration_seconds=240.0,
        )

    def request(self, **changes: object) -> MusicGenerationRequest:
        values: dict[str, object] = {
            "schema_version": 1,
            "request_id": "request_s05",
            "project_id": ProjectId("project_s05"),
            "contract_checksum": "a" * 64,
            "source_revision_ids": (
                RevisionId("rev_brief"),
                RevisionId("rev_blueprint"),
                RevisionId("rev_lyrics"),
                RevisionId("rev_plan"),
            ),
            "backend_id": "heartmula_3b",
            "backend_revision": "source-pin",
            "operation": "text2music",
            "language": "vi",
            "vocal_mode": VocalMode.VOCAL,
            "prompt": "Vietnamese dream pop with warm synths",
            "lyrics": "[Verse]\nMình đi qua đêm dài",
            "duration_seconds": 30.0,
            "takes": (
                TakeSpec("take_faithful", "faithful", 11, {"temperature": 0.9}),
                TakeSpec("take_expressive", "expressive", 22, {"temperature": 1.0}),
            ),
        }
        values.update(changes)
        return MusicGenerationRequest(**values)  # type: ignore[arg-type]

    def test_valid_approved_batch_executes_and_preserves_identity(self) -> None:
        backend = StubBackend(self.descriptor)
        events: list[dict[str, object]] = []
        result = execute_generation(backend, self.request(), events.append)
        self.assertEqual(1, backend.calls)
        self.assertEqual(
            ("take_faithful", "take_expressive"), tuple(x.take_id for x in result.takes)
        )
        self.assertEqual("contract_validated", events[0]["stage"])
        self.assertEqual("batch_validated", events[-1]["stage"])

    def test_vocal_and_instrumental_intent_cannot_be_silently_changed(self) -> None:
        backend = StubBackend(self.descriptor)
        with self.assertRaisesRegex(GenerationContractError, "requires approved lyrics"):
            execute_generation(backend, self.request(lyrics=None), lambda _event: None)
        with self.assertRaisesRegex(GenerationContractError, "must not hide lyrics"):
            execute_generation(
                backend,
                self.request(vocal_mode=VocalMode.INSTRUMENTAL),
                lambda _event: None,
            )
        self.assertEqual(0, backend.calls)

    def test_unverified_language_or_capability_fails_before_backend_call(self) -> None:
        backend = StubBackend(self.descriptor)
        with self.assertRaisesRegex(UnsupportedGenerationError, "language"):
            execute_generation(backend, self.request(language="fr"), lambda _event: None)
        with self.assertRaisesRegex(UnsupportedGenerationError, "instrumental"):
            execute_generation(
                backend,
                self.request(vocal_mode=VocalMode.INSTRUMENTAL, lyrics=None),
                lambda _event: None,
            )
        self.assertEqual(0, backend.calls)

    def test_instrumental_request_does_not_claim_lyric_language_support(self) -> None:
        descriptor = MusicBackendDescriptor(
            backend_id="instrumental_backend",
            source_revision="source-pin",
            model_ids=("model@revision",),
            license_ids=("MIT",),
            operations=frozenset({"text2music"}),
            supported_languages=frozenset({"und"}),
            supports_vocals=False,
            supports_instrumental=True,
            min_duration_seconds=20.0,
            max_duration_seconds=240.0,
        )
        request = self.request(
            backend_id="instrumental_backend",
            vocal_mode=VocalMode.INSTRUMENTAL,
            lyrics=None,
        )
        result = execute_generation(StubBackend(descriptor), request, lambda _event: None)
        self.assertEqual(2, len(result.takes))

    def test_take_batch_requires_unique_ids_seeds_and_two_to_four_items(self) -> None:
        backend = StubBackend(self.descriptor)
        duplicate = TakeSpec("take_faithful", "duplicate", 11, {})
        with self.assertRaisesRegex(GenerationContractError, "Take IDs"):
            execute_generation(
                backend,
                self.request(takes=(self.request().takes[0], duplicate)),
                lambda _event: None,
            )
        with self.assertRaisesRegex(GenerationContractError, "two to four"):
            execute_generation(
                backend,
                self.request(takes=(self.request().takes[0],)),
                lambda _event: None,
            )
        self.assertEqual(0, backend.calls)

    def test_registry_rejects_unknown_or_changed_revision(self) -> None:
        backend = StubBackend(self.descriptor)
        registry = MusicBackendRegistry()
        registry.register(backend)
        self.assertIs(backend, registry.resolve("heartmula_3b", "source-pin"))
        with self.assertRaisesRegex(UnsupportedGenerationError, "Unknown"):
            registry.resolve("other", "source-pin")
        with self.assertRaisesRegex(UnsupportedGenerationError, "revision"):
            registry.resolve("heartmula_3b", "changed")

    def test_incomplete_or_invalid_backend_output_is_not_a_success(self) -> None:
        class IncompleteBackend(StubBackend):
            def generate(self, request, on_progress):  # type: ignore[no-untyped-def]
                result = super().generate(request, on_progress)
                return GenerationBatchResult(
                    result.backend_id,
                    result.backend_revision,
                    result.contract_checksum,
                    result.takes[:1],
                    result.metrics,
                )

        with self.assertRaisesRegex(GenerationContractError, "incomplete"):
            execute_generation(IncompleteBackend(self.descriptor), self.request(), lambda _: None)

    def test_request_serialization_round_trip_is_strict(self) -> None:
        request = self.request()
        self.assertEqual(request, request_from_json(request_to_json(request)))
        payload = request_to_json(request)
        payload["takes"] = "not-an-array"
        with self.assertRaisesRegex(GenerationContractError, "object array"):
            request_from_json(payload)

    def test_result_serialization_round_trip_is_strict(self) -> None:
        result = StubBackend(self.descriptor).generate(self.request(), lambda _event: None)
        self.assertEqual(result, result_from_json(result_to_json(result)))
        payload = result_to_json(result)
        payload["metrics"] = "not-an-object"
        with self.assertRaisesRegex(GenerationContractError, "metrics"):
            result_from_json(payload)


if __name__ == "__main__":
    unittest.main()
