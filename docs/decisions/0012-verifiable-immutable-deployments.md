# ADR 0012: Verifiable immutable deployments

Status: accepted; phase one and phase-two manifest gate implemented, activation planned
Date: 2026-09-04

## Context

Long-running Controller, sender, and provider workers can start at different
times while the source checkout and its in-tree virtual environment continue to
change. Process liveness then does not prove that components execute one
revision. Python may also load a previously unimported module after startup, so
a mutable checkout can produce a single process containing code from different
revisions.

The package version describes a release line but does not identify an exact
commit, dirty tree, configuration revision, migration, or executable artifact.
Operational acceptance cannot be attributed or reproduced without this
provenance.

## Decision

1. Every runnable artifact has an immutable release identity containing package
   version, exact Git commit, build time, and a clean-tree assertion. A dirty
   tree is a development build and cannot be promoted.
2. Every Controller, sender, monitor, and provider worker publishes that release
   identity in bounded runtime health. Monitoring reports a mixed-revision
   deployment once and re-arms only after convergence.
3. Deployment uses an artifact or revision-specific release directory. Services
   never execute production code from the mutable development checkout.
4. A private deployment manifest binds release identity, configuration digest,
   schema version, activation time, previous compatible release, and backup
   reference. It contains no credential values.
5. Promotion is staged: validate artifact, drain or classify work, create and
   verify a consistent backup, stop admission, activate the selected release,
   start execution and delivery components in the documented order, start
   admission last, then run health and live smoke gates.
6. Rollback selects the previous immutable artifact and a schema-compatible
   configuration. It never reconstructs a prior release by editing the current
   checkout.
7. Git SHA is the authoritative deployment identity. Semantic versions and tags
   describe releases but never substitute for the SHA.

## Phased delivery

The first phase adds embedded revision metadata, per-process health reporting,
and mixed/unknown-revision detection. Phase two now includes a private,
digest-bound deployment manifest and a read-only schema-compatibility gate for
distinct active and rollback wheels. Revision-specific installation and the
controlled release-pointer switch remain planned. Packaging automation must
remain simpler than a container platform; containers are not required for this
single-machine deployment.

## Consequences

- `active` no longer means `current`; health can prove both liveness and code
  convergence.
- Restart and E2E evidence can name the exact code under test.
- Deployment takes an explicit controlled step instead of inheriting arbitrary
  edits from a developer checkout.
- Disk use increases by a small bounded number of retained releases. Retention
  must keep the active release and at least one schema-compatible rollback.
- Secrets, provider state, Telegram state, and project data remain outside the
  artifact and are not copied into a release directory.
