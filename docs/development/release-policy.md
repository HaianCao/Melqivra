# Versioning and release policy

Agent Music is pre-release. Stage tags are evidence checkpoints; they are not
package releases.

## Versioning

- Public releases follow Semantic Versioning once `v0.1.0-rc1` exists.
- Minor `0.y` releases may evolve APIs but require migration and release notes.
- Patch releases contain compatible fixes and security updates.
- OpenAPI, project archive, database schema, generation manifest, and tool schemas
  are versioned public artifacts when introduced.

## Release process

1. Confirm roadmap scope and stage evidence.
2. Freeze dependency/model revisions and regenerate the SBOM.
3. Run fresh-machine, migration, backup/restore, security, license, accessibility,
   T4, and supported local-GPU gates applicable to the release.
4. Publish a release candidate with checksums and known limitations.
5. Fix or explicitly block on release-critical findings.
6. Tag the reviewed commit and publish immutable release notes/artifacts.

Releases are built from a clean tagged commit. Credentials, private test data,
large generated audio, and mutable external URLs are not release artifacts.

## Rollback

Code rollback alone is insufficient when schemas or artifacts changed. Every
such change documents forward migration, backup requirements, compatibility
window, and restore procedure before merge.

