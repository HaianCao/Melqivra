"""Capability-only backend proposals that always require client approval."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_music.backends.ace_step import ACE_STEP_INSTRUMENTAL_DESCRIPTOR
from agent_music.backends.heartmula import HEARTMULA_DESCRIPTOR
from agent_music.generation import MusicBackendDescriptor, VocalMode


class BackendReleaseStatus(StrEnum):
    """Product maturity of one exact backend capability pin."""

    VERIFIED_CANDIDATE = "verified_candidate"
    RESEARCH_ONLY = "research_only"


@dataclass(frozen=True, slots=True)
class BackendPolicy:
    """One descriptor plus its product release restriction."""

    descriptor: MusicBackendDescriptor
    status: BackendReleaseStatus
    note: str


@dataclass(frozen=True, slots=True)
class BackendIntent:
    """The user-approved capability requirements used for routing."""

    operation: str
    language: str
    vocal_mode: VocalMode
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class BackendOption:
    """A compatible backend option; selection remains a client decision."""

    backend_id: str
    backend_revision: str
    status: BackendReleaseStatus
    note: str


@dataclass(frozen=True, slots=True)
class BackendProposal:
    """Ordered compatible choices with no implicit execution or fallback."""

    intent: BackendIntent
    options: tuple[BackendOption, ...]
    requires_client_approval: bool = True


DEFAULT_BACKEND_POLICIES = (
    BackendPolicy(
        HEARTMULA_DESCRIPTOR,
        BackendReleaseStatus.VERIFIED_CANDIDATE,
        "Real T4 vocal batch passed; narrow VRAM margin and five declared languages.",
    ),
    BackendPolicy(
        ACE_STEP_INSTRUMENTAL_DESCRIPTOR,
        BackendReleaseStatus.RESEARCH_ONLY,
        "Real T4 30-second instrumental passed; ADR-0004 forbids vocal routing.",
    ),
)


def propose_backends(
    intent: BackendIntent,
    *,
    policies: tuple[BackendPolicy, ...] = DEFAULT_BACKEND_POLICIES,
) -> BackendProposal:
    """Return every exact compatible pin, ordered by product maturity."""

    if not intent.operation.strip() or not intent.language.strip():
        raise ValueError("Backend intent operation and language are required")
    if intent.duration_seconds <= 0:
        raise ValueError("Backend intent duration must be positive")
    options: list[BackendOption] = []
    for policy in policies:
        descriptor = policy.descriptor
        descriptor.validate()
        if intent.operation not in descriptor.operations:
            continue
        if (
            not descriptor.min_duration_seconds
            <= intent.duration_seconds
            <= descriptor.max_duration_seconds
        ):
            continue
        if intent.vocal_mode is VocalMode.VOCAL:
            supported = {language.casefold() for language in descriptor.supported_languages}
            if not descriptor.supports_vocals or intent.language.casefold() not in supported:
                continue
        elif not descriptor.supports_instrumental:
            continue
        options.append(
            BackendOption(
                descriptor.backend_id,
                descriptor.source_revision,
                policy.status,
                policy.note,
            )
        )
    rank = {
        BackendReleaseStatus.VERIFIED_CANDIDATE: 0,
        BackendReleaseStatus.RESEARCH_ONLY: 1,
    }
    options.sort(key=lambda option: (rank[option.status], option.backend_id))
    return BackendProposal(intent, tuple(options))
