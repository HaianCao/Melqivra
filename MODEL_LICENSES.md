# Model and weight license inventory

This inventory is separate from Melqivra's Apache-2.0 source-code license.
Repository licensing does not grant permission to redistribute model weights,
datasets, codecs, reference audio, or generated artifacts.

Status: S01 candidate review; no model assets are bundled or approved for a
Melqivra release yet.

## ACE-Step 1.5 source

- Project: ACE-Step 1.5
- Upstream: <https://github.com/ace-step/ACE-Step-1.5>
- Candidate release: `v0.1.8`
- Pinned commit: `dce621408bee8c31b4fcf4811682eb9359e1bc94`
- Declared license: MIT
- License evidence: upstream `LICENSE` at the pinned commit and GitHub license
  metadata
- Intended use: benchmark-only source for the S01 T4 decision
- Redistribution decision: not bundled; retrieve from the upstream source by
  exact revision

## ACE-Step 1.5 turbo model bundle

- Repository: <https://huggingface.co/ACE-Step/Ace-Step1.5>
- Pinned revision: `19671f406d603126926c1b7e2adc169acbcade22`
- Declared model-card license: MIT
- Candidate components:
  - `acestep-v15-turbo/model.safetensors`
  - `vae/diffusion_pytorch_model.safetensors`
  - `Qwen3-Embedding-0.6B/model.safetensors`
- Intended use: text-to-music, reference, cover, and repaint capability
  benchmark on a Colab T4
- Redistribution decision: not bundled; cache only for the user who retrieved
  the assets, with exact revision and checksum metadata
- Open review item: verify the transitive terms and attribution requirements of
  the embedded Qwen3-derived component before release approval

## ACE-Step 5 Hz language model 0.6B

- Repository: <https://huggingface.co/ACE-Step/acestep-5Hz-lm-0.6B>
- Pinned revision: `148d8ea0225bdab342ee1ae3a354275ccd60ca80`
- Declared model-card license: MIT
- Base architecture: Qwen3 0.6B derivative, according to the model metadata
- Intended use: explicit LM-on capability benchmark on T4; a failed or disabled
  initialization must fail the requested capability rather than silently use
  DiT-only generation
- Redistribution decision: not bundled; retrieve by revision and verify the
  cached file checksum
- Open review item: verify base-model notices and attribution before release
  approval

## Current decision boundary

These records establish reproducible candidates, not a shipping decision. S01
must still prove the exact assets on a fresh T4, validate the upstream and
transitive license terms, record cache manifests, and accept a backend ADR.
Alternative models require their own code, weight, dataset, and output-terms
review before they can enter the default product path.
