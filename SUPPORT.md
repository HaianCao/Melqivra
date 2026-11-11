# Support

Melqivra is currently pre-release and maintained on a best-effort basis.

## Where to ask

- Use GitHub Issues for reproducible bugs and scoped feature requests.
- Use GitHub Discussions for setup help, design questions, and ideas.
- Use private vulnerability reporting for security issues.

Before filing a bug, run `melqivra doctor`, remove secrets from the output,
and search existing reports.

Include the commit, operating system, Python version, installation method,
runner type, exact command, expected behavior, actual behavior, and a minimal
reproduction. For GPU issues include the model revision and non-sensitive doctor
output. Do not upload private audio or credentials.

## Current support boundaries

- CPU-only FakeRunner development is the most stable path.
- Colab support targets the official CLI from Linux/WSL2 and remains experimental.
- Native Windows Colab control is not currently supported.
- Local CUDA execution is planned but not yet implemented.
- The current identity header is for development only and is not production auth.
- Model quality, runtime availability, and Colab quotas are not guaranteed.

See the active GitHub milestones and `docs/evidence/` for completion status and
explicit limitations.

