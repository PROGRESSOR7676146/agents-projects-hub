# Source release 0.8.0

Status: source-release scope; deployment and live acceptance remain open
Date: 2026-10-13. Owner: Hub maintainer (integration), repository owner (promotion).

This release integrates the latest schema-53 source stack based on `f2dcfdf`,
the immutable packaging published in `v0.7.1` at `ba0181a`, and the saved-session
placeholder correction from `6555911`. The release uses the standard
[metadata and publication gate](RELEASE_METADATA.md). The PR and tag's artifact
inventory own the exact resulting revision and its publication evidence.

## Scope and dependencies

The source includes Codex transport/control-loss fences, explicit delivery and
retry reconciliation, protected Claude editor/custody adapters, the passive
incident journal and optional Hermes incident cards. Claude stays text-only by
default; Hermes cards stay disabled without their local configuration. Existing
native-human approval, custody, subscription-route and live acceptance gates
remain independent of publication.

[ADR 0065](../decisions/0065-retire-hub-lead-advisor.md) still retires Hub roles.
Integration retains the dormant primitives and tests under the checked
[dependency inventory](RETAINED_PROVIDER_DEPENDENCIES.md); it adds no runtime
role wiring. The independent stack-update tool remains separate pending work;
source release does not imply an update-plane or machine-loss recovery drill.

## Artifact and rollback gate

Every release publishes the clean-SHA wheel, complete tracked-source archive,
runtime/build locks, public inventory and checksums. Download the hosted assets
into a fresh directory, verify all digests, then verify the wheel against the
tag's exact clean revision. A temporary installation and the actual-wheel
[rollout/rollback rehearsal](IMMUTABLE_RELEASES.md#automated-offline-rollout-and-rollback)
are offline evidence; they neither activate services nor migrate live state.

The source supports schema 53. Version 0.7.1 supports schema 41 and cannot serve
as its ordinary post-migration runtime rollback. Prepare and verify a distinct
clean schema-53-compatible wheel by source SHA and digest. Schema compatibility
alone does not prove configuration, custody or native session-mode rollback.
The [recovery plan](WSL_OFF_MACHINE_RECOVERY.md) owns the off-machine inventory
and cold-restore criteria; locks alone do not retain dependency distributions.

## Continuation

Lane: `release/latest-0.8.0`, sole writer Hub maintainer, based on `f2dcfdf`.
Purpose: integrate the reviewed source and package it for main promotion.
Inspect tracked, staged and untracked state after promotion before any cleanup.
Next trigger: exact-candidate canonical and hosted checks, independent combined
source review, main promotion and hosted-asset verification. Live rollout,
three-project acceptance and cold recovery remain separately authorized tasks.
