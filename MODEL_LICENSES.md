# Model and weight license inventory

This inventory is separate from Melqivra's Apache-2.0 source-code license.
Repository licensing does not grant permission to redistribute model weights,
datasets, codecs, reference audio, or generated artifacts.

Status: S01 review complete for non-bundled research use. No model assets are
approved for redistribution in a Melqivra release.

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
- Transitive boundary: the corresponding upstream Qwen3 Embedding 0.6B and
  Qwen3 0.6B Base repositories declare Apache-2.0. Preserve that license and
  attribution in release notices even though Melqivra does not bundle weights.

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
- Transitive boundary: Qwen3 0.6B Base declares Apache-2.0. Preserve the base
  attribution and license in release notices; do not infer that the ACE-Step
  model-card MIT declaration removes upstream notice obligations.

## HeartMuLa source and model assets

- Source repository: <https://github.com/HeartMuLa/heartlib>
- Pinned source revision: `5858ca8f1ffe58d62be7c007ea55f1033c5ca456`
- Source license: Apache-2.0
- Base metadata/tokenizer repository:
  `HeartMuLa/HeartMuLaGen@2e18e01702011f4f7dc8642b6260967df62417ea`
- Music model repository:
  `HeartMuLa/HeartMuLa-oss-3B-happy-new-year@00a79ac9477f64593eba2ca96a25807cb2f55be6`
- Codec repository:
  `HeartMuLa/HeartCodec-oss-20260123@f889dab0532cfa4bf459f2a3367eb6d346b8eeda`
- Declared source/model-card license: Apache-2.0
- Intended use: non-bundled S05 vocal generation for the exact languages and
  capability pins that have passed the product contract
- Redistribution decision: not bundled; operators retrieve exact upstream
  revisions into their own private cache, and generated manifests preserve all
  three model identifiers plus license provenance

The license declaration does not expand the model's verified language,
instrumental, voice-identity, or hardware capability. Reference media and user
lyrics retain their own rights and consent requirements.

## ACE-Step community float32 research fork

- Repository: <https://github.com/peter571/ACE-Step-1.5>
- Pinned revision: `44c15413f6dc9b55acfbd214209dbb6c0aac74b9`
- Pinned upstream base: `dce621408bee8c31b4fcf4811682eb9359e1bc94`
- Declared source license boundary: ACE-Step MIT fork; model assets remain the
  separately pinned upstream repositories above
- Intended use: one explicitly approved, non-bundled Vietnamese
  reference-conditioned float32 research diagnostic
- Redistribution decision: not bundled; no product capability claim exists
  until the exact fork passes real-T4 evidence

## Current decision boundary

ADR-0004 rejects the pinned ACE-Step candidate as the default production backend
and retains it for non-bundled research only. ADR-0006 permits a catalog of
separately pinned, capability-scoped backends, with HeartMuLa currently verified
only for its declared vocal languages. Operators may retrieve exact upstream
revisions into their own cache; Melqivra releases do not contain or mirror the
weights.

Release notices must preserve the ACE-Step MIT notice, identify the Qwen3
Apache-2.0 lineage, and preserve HeartMuLa/HeartCodec Apache-2.0 attribution
when those adapters are distributed. Generated artifacts record model and
license provenance, but no ownership or unrestricted-use guarantee is inferred
from a model-card license. Alternative models, redistributed weights, or changed
output terms require a new code, weight, dataset, and license review before
entering the product path.
