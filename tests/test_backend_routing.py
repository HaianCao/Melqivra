"""Explicit multi-backend capability routing tests."""

from __future__ import annotations

import unittest

from agent_music.backend_routing import (
    BackendIntent,
    BackendReleaseStatus,
    propose_backends,
)
from agent_music.generation import VocalMode


class BackendRoutingTests(unittest.TestCase):
    def test_verified_language_vocal_proposes_heartmula_only(self) -> None:
        proposal = propose_backends(BackendIntent("text2music", "en", VocalMode.VOCAL, 30.0))
        self.assertTrue(proposal.requires_client_approval)
        self.assertEqual(("heartmula_3b",), tuple(option.backend_id for option in proposal.options))
        self.assertEqual(BackendReleaseStatus.VERIFIED_CANDIDATE, proposal.options[0].status)

    def test_vietnamese_vocal_has_no_silent_backend_fallback(self) -> None:
        proposal = propose_backends(BackendIntent("text2music", "vi", VocalMode.VOCAL, 30.0))
        self.assertEqual((), proposal.options)

    def test_instrumental_proposes_only_exact_verified_ace_contract(self) -> None:
        proposal = propose_backends(BackendIntent("text2music", "vi", VocalMode.INSTRUMENTAL, 30.0))
        self.assertEqual(("ace_step_v15_turbo",), tuple(x.backend_id for x in proposal.options))
        self.assertEqual(BackendReleaseStatus.RESEARCH_ONLY, proposal.options[0].status)
        self.assertEqual(
            (),
            propose_backends(
                BackendIntent("text2music", "vi", VocalMode.INSTRUMENTAL, 45.0)
            ).options,
        )


if __name__ == "__main__":
    unittest.main()
