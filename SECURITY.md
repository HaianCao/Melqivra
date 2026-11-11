# Security Policy

## Supported versions

Melqivra is pre-release software. Until v0.1.0, only the latest commit on the
default branch receives security fixes. A supported-version table will replace
this rule at the first stable release.

## Reporting a vulnerability

Do not create a public issue, discussion, or pull request containing exploit
details, credentials, private artifacts, or personal data.

Use GitHub private vulnerability reporting when it is enabled for the project.
If it is unavailable, contact the maintainers through the private security
contact published on the repository profile. The repository must configure a
concrete monitored address before v0.1.0-rc1.

Include only what is necessary:

- affected commit/version and configuration;
- impact and required attacker access;
- minimal reproduction or proof of concept;
- suggested mitigation if known;
- whether the issue has been disclosed elsewhere.

Expected handling targets are acknowledgment within 72 hours, an initial
assessment within seven days, and coordinated disclosure after a fix or agreed
mitigation. These are targets, not a bounty or legal guarantee.

## High-risk boundaries

Particularly sensitive reports include:

- planner/tool authorization or approval bypass;
- raw command, path traversal, unsafe archive/media parsing, or prompt injection;
- cross-user project, job, artifact, or voice-profile access;
- leaked OpenAI-compatible, Colab, storage, or database credentials;
- duplicate GPU execution, stale-worker finalization, or data-loss conditions;
- dependency/model supply-chain compromise;
- consent, revocation, deletion, or voice-identity control bypass.

## Secrets and test data

Never attach real credentials or private user audio/lyrics to a report. Revoke
any credential accidentally exposed and use synthetic fixtures when possible.

## Safe harbor

Good-faith research that avoids privacy violations, service disruption, data
destruction, social engineering, and unnecessary access will be handled
constructively. This policy does not authorize testing systems or accounts that
you do not own or have permission to test.

