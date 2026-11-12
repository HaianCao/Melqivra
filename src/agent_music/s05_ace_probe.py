"""CPU-safe authorization contract for the bounded ACE reference probe."""

from __future__ import annotations

from collections.abc import Mapping

ACE_FLOAT32_REPOSITORY = "https://github.com/peter571/ACE-Step-1.5.git"
ACE_FLOAT32_REVISION = "44c15413f6dc9b55acfbd214209dbb6c0aac74b9"
ACE_UPSTREAM_BASE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"


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
    if authorization.get("source_revision") != ACE_FLOAT32_REVISION:
        raise ValueError("Authorization selected a different source revision")
    if authorization.get("reference_sha256") != reference_sha256:
        raise ValueError("Authorization selected different reference audio")
    if authorization.get("private_inputs_sha256") != private_inputs_sha256:
        raise ValueError("Authorization selected different private inputs")
