# Stabilization release 0.7.1

Status: release candidate; publication and exact artifact checks pending
Source baseline: `27a88260a58f985b35317427a4381746414642f3`
Owner: Hub maintainer (packaging and source); repository owner (release decisions)

This maintenance release freezes the schema-41 source baseline and adds complete
public installation assets. It does not enable an opt-in provider capability or
establish deployment, human-approval or machine-loss recovery acceptance.

## Contents and acceptance

The release publishes a wheel built directly from an exact clean Git checkout,
a source archive, `requirements-release.lock`, `uv.lock`, `release-info.json`
and `SHA256SUMS`. The public inventory includes artifact names, sizes and digests,
package version, source SHA, clean assertion, build time and schema range. It is
not a private deployment manifest and contains no deployment paths or state.

The wheel is the installable immutable artifact. Building a wheel from the source
archive does not carry verified Git provenance and must not replace it for
activation. Runtime dependency installation follows the locked procedure in
[immutable releases](IMMUTABLE_RELEASES.md#reproducible-runtime-dependencies).
The lock pins resolution; offline recovery must also retain its dependency
artifacts outside the machine.

Publication requires canonical checks and independent review of release plumbing.
The actual candidate and a distinct compatible schema-41 rollback wheel must pass
[the synthetic rollout/rollback gate](IMMUTABLE_RELEASES.md#automated-offline-rollout-and-rollback).
After hosted publication, download the assets into a fresh directory, verify
`SHA256SUMS`, and inspect the wheel identity against the tag's exact clean SHA.
For a release built on a maintenance branch, no `main` merge is implied.

## Scope

The baseline retains exact-session connection, transport deadlines, saved-task
retry and managed-profile continuity. Text-only Claude remains the default;
protected file tools retain all existing local opt-in, custody, native-human
approval and route acceptance gates. Hub role orchestration is outside this
stabilization release. Newer incident/control candidates and the schema-53 stack
remain separate source work, not accepted capabilities of this release.

Publishing packages neither stops a service nor migrates a live database.
Installation, live canaries and an off-machine backup/restore remain separate
operations. The [machine-loss plan](WSL_OFF_MACHINE_RECOVERY.md) owns the complete
recovery-set inventory and cold-drill criteria.

## Continuation and closure

Implementation lane: `release/stabilization-0.7.1`, based on the source SHA above.
Purpose: freeze and package the bounded maintenance release. After publication,
inspect tracked, staged and untracked files before any worktree cleanup.
Next trigger: exact source gates, real wheel/rollback rehearsal, publication and
download verification. Closure requires the release URL, exact tag SHA, asset
checksums and named automated evidence; live acceptance remains separate.
