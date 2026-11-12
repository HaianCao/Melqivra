"""Conservative ACE-Step descriptor for the verified instrumental capability."""

from __future__ import annotations

from agent_music.generation import MusicBackendDescriptor

ACE_STEP_SOURCE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
ACE_STEP_TURBO_MODEL_ID = "ACE-Step/Ace-Step1.5@19671f406d603126926c1b7e2adc169acbcade22"
ACE_STEP_VAE_MODEL_ID = "ACE-Step/Ace-Step1.5:vae@19671f406d603126926c1b7e2adc169acbcade22"

ACE_STEP_INSTRUMENTAL_DESCRIPTOR = MusicBackendDescriptor(
    backend_id="ace_step_v15_turbo",
    source_revision=ACE_STEP_SOURCE_REVISION,
    model_ids=(ACE_STEP_TURBO_MODEL_ID, ACE_STEP_VAE_MODEL_ID),
    license_ids=("MIT", "Apache-2.0"),
    operations=frozenset({"text2music"}),
    supported_languages=frozenset({"und"}),
    supports_vocals=False,
    supports_instrumental=True,
    min_duration_seconds=30.0,
    max_duration_seconds=30.0,
)
