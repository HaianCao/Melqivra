"""Strategy-registry tests for interchangeable GPU providers."""

from __future__ import annotations

import unittest

from agent_music.config import HarnessSettings
from agent_music.runners.colab import ColabRunner
from agent_music.runners.fake import FakeRunner
from agent_music.runners.local import LocalCudaRunner
from agent_music.runners.modal import ModalRunner
from agent_music.runners.strategy import DEFAULT_RUNNER_STRATEGIES, build_runner


class RunnerStrategyTests(unittest.TestCase):
    def test_default_registry_exposes_all_explicit_strategies(self) -> None:
        self.assertEqual(
            ("modal", "colab", "fake", "local"),
            DEFAULT_RUNNER_STRATEGIES.names,
        )

    def test_configuration_selects_one_strategy_without_fallback(self) -> None:
        expected = {
            "modal": ModalRunner,
            "colab": ColabRunner,
            "fake": FakeRunner,
            "local": LocalCudaRunner,
        }
        for name, runner_type in expected.items():
            with self.subTest(name=name):
                runner = build_runner(HarnessSettings(runner=name))
                self.assertIsInstance(runner, runner_type)

    def test_unknown_strategy_is_rejected(self) -> None:
        settings = HarnessSettings(runner="unknown")
        with self.assertRaisesRegex(ValueError, "Unknown GPU runner strategy"):
            build_runner(settings)


if __name__ == "__main__":
    unittest.main()
