"""Authorization and public-price contract for the bounded S06 T4 probe."""

from __future__ import annotations

from collections.abc import Mapping

MODAL_PRICING_SOURCE = "https://modal.com/pricing"
MODAL_PRICE_CHECKED_AT = "2026-11-13"
MODAL_T4_USD_PER_SECOND = 0.000164
MODAL_CPU_USD_PER_CORE_SECOND = 0.0000131
MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000222
PROBE_GPU_CALLS = 3
PROBE_MAX_FUNCTION_SECONDS = 120
PROBE_CPU_CORES = 2
PROBE_MEMORY_GIB = 1


def estimated_max_metered_usd() -> float:
    """Return the list-price upper bound for all bounded probe calls."""

    compute_per_second = (
        MODAL_T4_USD_PER_SECOND
        + (PROBE_CPU_CORES * MODAL_CPU_USD_PER_CORE_SECOND)
        + (PROBE_MEMORY_GIB * MODAL_MEMORY_USD_PER_GIB_SECOND)
    )
    return round(
        compute_per_second * PROBE_MAX_FUNCTION_SECONDS * PROBE_GPU_CALLS,
        6,
    )


def validate_probe_authorization(
    authorization: Mapping[str, object],
    *,
    expected_run_id: str,
) -> None:
    """Reject execution unless provider, price, bound, and run ID are approved."""

    if authorization.get("gpu_execution_approved") is not True:
        raise ValueError("Explicit GPU execution approval is required")
    if authorization.get("provider_price_prechecked") is not True:
        raise ValueError("Provider price precheck approval is required")
    ceiling = authorization.get("approved_max_metered_usd")
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or ceiling <= 0:
        raise ValueError("A positive approved provider cost ceiling is required")
    estimate = estimated_max_metered_usd()
    if ceiling < estimate:
        raise ValueError("Approved provider cost ceiling is below the bounded estimate")
    expected = {
        "provider": "modal",
        "provider_price_source": MODAL_PRICING_SOURCE,
        "provider_price_checked_at": MODAL_PRICE_CHECKED_AT,
        "probe_gpu_calls": PROBE_GPU_CALLS,
        "max_function_seconds": PROBE_MAX_FUNCTION_SECONDS,
        "estimated_max_metered_usd": estimate,
        "run_id": expected_run_id,
    }
    for name, value in expected.items():
        if authorization.get(name) != value:
            raise ValueError(f"Authorization selected a different {name}")
