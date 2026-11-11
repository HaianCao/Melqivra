# Development support matrix

This matrix describes tested intent, not a claim that unfinished runners work.
Roadmap stage status remains authoritative.

| Environment | Python | FakeRunner | Colab broker | Local CUDA |
|---|---:|---|---|---|
| Windows 11 | 3.11, 3.13 | Supported for development | Use WSL2 | Not implemented |
| Ubuntu latest CI | 3.11, 3.13 | Supported for development | Experimental, S06 | Not implemented |
| WSL2 Ubuntu | 3.11, 3.13 | Supported for development | Experimental | Not implemented |
| Docker CPU profile | Image Python | Supported | Not bundled | Not implemented |
| macOS | 3.11–3.13 | Best effort | CLI may work but is not CI-tested | Not planned for CUDA |

“Supported for development” means the locked environment, doctor, tests, and
fake harness are expected to work. It does not mean production authentication,
multi-tenant isolation, or real music generation is complete.

Python 3.12 is inside the declared package range and is used by the current
Docker image. The mandatory CI boundary versions are 3.11 and 3.13.

## Platform policy

- Add a platform only with a reproducible evidence run.
- Remove support only through a changelog and roadmap update.
- Do not infer GPU support from successful CPU tests.
- Colab credentials must remain in the Linux/WSL environment running the broker.
- Hardware claims require a model, driver, VRAM, precision, and benchmark record.

