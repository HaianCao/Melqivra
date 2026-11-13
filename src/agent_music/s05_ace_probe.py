"""CPU-safe authorization contract for the bounded ACE reference probe."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

ACE_FLOAT32_REPOSITORY = "https://github.com/peter571/ACE-Step-1.5.git"
ACE_FLOAT32_REVISION = "44c15413f6dc9b55acfbd214209dbb6c0aac74b9"
ACE_UPSTREAM_BASE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
MODAL_PRICING_SOURCE = "https://modal.com/pricing"
MODAL_PRICE_CHECKED_AT = "2026-11-13"
MODAL_T4_USD_PER_SECOND = 0.000164
MODAL_CPU_USD_PER_CORE_SECOND = 0.0000131
MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000222
MODAL_EGRESS_USD_PER_GIB = 0.04
PROBE_CPU_CORES = 4
PROBE_MEMORY_GIB = 24
PROBE_MAX_FUNCTION_SECONDS = 700
PROBE_MAX_TRANSFER_BYTES = 10_000_000


def estimated_max_metered_usd() -> float:
    """Return the public-list-price upper bound for one bounded probe call."""

    compute_per_second = (
        MODAL_T4_USD_PER_SECOND
        + (PROBE_CPU_CORES * MODAL_CPU_USD_PER_CORE_SECOND)
        + (PROBE_MEMORY_GIB * MODAL_MEMORY_USD_PER_GIB_SECOND)
    )
    egress = (PROBE_MAX_TRANSFER_BYTES / (1024**3)) * MODAL_EGRESS_USD_PER_GIB
    return round((compute_per_second * PROBE_MAX_FUNCTION_SECONDS) + egress, 6)


def decode_exact_utf8(payload: bytes) -> tuple[str, str]:
    """Decode UTF-8 without newline translation and return its exact SHA-256."""

    return payload.decode("utf-8"), hashlib.sha256(payload).hexdigest()


def validate_probe_authorization(
    authorization: Mapping[str, object],
    *,
    reference_sha256: str,
    private_inputs_sha256: str,
) -> None:
    """Reject execution unless consent, cost, revision, and private hashes match."""

    required_true = (
        "gpu_execution_approved",
        "reference_media_reuse_approved",
        "provider_price_prechecked",
    )
    if not all(authorization.get(name) is True for name in required_true):
        raise ValueError("GPU, reference-media, and provider-price approvals are required")
    ceiling = authorization.get("approved_max_metered_usd")
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or ceiling <= 0:
        raise ValueError("A positive approved provider cost ceiling is required")
    expected_estimate = estimated_max_metered_usd()
    if ceiling < expected_estimate:
        raise ValueError("Approved provider cost ceiling is below the bounded price estimate")
    if authorization.get("provider") != "modal":
        raise ValueError("Authorization selected a different provider")
    if authorization.get("provider_price_source") != MODAL_PRICING_SOURCE:
        raise ValueError("Authorization used a different provider price source")
    if authorization.get("provider_price_checked_at") != MODAL_PRICE_CHECKED_AT:
        raise ValueError("Authorization used a different provider price-check date")
    if authorization.get("max_function_seconds") != PROBE_MAX_FUNCTION_SECONDS:
        raise ValueError("Authorization selected a different function timeout")
    if authorization.get("estimated_max_metered_usd") != expected_estimate:
        raise ValueError("Authorization selected a different bounded price estimate")
    if authorization.get("source_revision") != ACE_FLOAT32_REVISION:
        raise ValueError("Authorization selected a different source revision")
    if authorization.get("reference_sha256") != reference_sha256:
        raise ValueError("Authorization selected different reference audio")
    if authorization.get("private_inputs_sha256") != private_inputs_sha256:
        raise ValueError("Authorization selected different private inputs")
