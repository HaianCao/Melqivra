"""Explicit strategy registry for interchangeable GPU execution providers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from agent_music.config import HarnessSettings
from agent_music.runners.base import GpuRunner
from agent_music.runners.colab import ColabRunner
from agent_music.runners.fake import FakeRunner
from agent_music.runners.local import LocalCudaRunner
from agent_music.runners.modal import ModalRunner


class RunnerStrategy(Protocol):
    """Construct one runner implementation without provisioning it."""

    @property
    def name(self) -> str: ...

    def build(self, settings: HarnessSettings) -> GpuRunner: ...


class ModalRunnerStrategy:
    """Build the default scale-to-zero Modal execution runner."""

    name = "modal"

    def build(self, settings: HarnessSettings) -> GpuRunner:
        return ModalRunner(settings)


class ColabRunnerStrategy:
    """Build the named-session Colab runner."""

    name = "colab"

    def build(self, settings: HarnessSettings) -> GpuRunner:
        return ColabRunner(settings)


class FakeRunnerStrategy:
    """Build the deterministic CPU-only development runner."""

    name = "fake"

    def build(self, settings: HarnessSettings) -> GpuRunner:
        return FakeRunner(job_seconds=settings.fake_job_seconds)


class LocalCudaRunnerStrategy:
    """Build the explicit local-CUDA placeholder."""

    name = "local"

    def build(self, settings: HarnessSettings) -> GpuRunner:
        _ = settings
        return LocalCudaRunner()


class RunnerStrategyRegistry:
    """Resolve configured runner names without provider-specific scheduler logic."""

    def __init__(self, strategies: Iterable[RunnerStrategy]) -> None:
        self._strategies: dict[str, RunnerStrategy] = {}
        for strategy in strategies:
            if strategy.name in self._strategies:
                raise ValueError(f"Duplicate runner strategy: {strategy.name}")
            self._strategies[strategy.name] = strategy

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._strategies)

    def build(self, settings: HarnessSettings) -> GpuRunner:
        try:
            strategy = self._strategies[settings.runner]
        except KeyError:
            available = ", ".join(self.names)
            raise ValueError(
                f"Unknown GPU runner strategy {settings.runner!r}; available: {available}"
            ) from None
        return strategy.build(settings)


DEFAULT_RUNNER_STRATEGIES = RunnerStrategyRegistry(
    (
        ModalRunnerStrategy(),
        ColabRunnerStrategy(),
        FakeRunnerStrategy(),
        LocalCudaRunnerStrategy(),
    )
)


def build_runner(
    settings: HarnessSettings,
    *,
    registry: RunnerStrategyRegistry = DEFAULT_RUNNER_STRATEGIES,
) -> GpuRunner:
    """Build the configured runner strategy without starting external resources."""

    return registry.build(settings)
