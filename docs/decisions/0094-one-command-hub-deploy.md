# ADR 0094: One-command Hub deploy

Status: proposed; first source slice implemented and fixture-tested

## Context

The installed Hub uses versioned paths in managed unit drop-ins and Claude
configuration. The prepared-release switcher in ADR 0089 operates on symlinks,
so it cannot by itself record and recover this set of file changes. A private
hand-adapted kit demonstrates the deployment sequence but is not a reusable
product interface.

## Decision

Provide a standalone standard-library tool, independent of the runtimes being
replaced, that discovers the installed revision, prepares a verified release,
and records a digest-bound plan and a recoverable service window. Private
installation information and deployment evidence remain outside the repository.
The window must back up state, verify rollback compatibility, preserve untouched
services, and verify convergence. An interrupted window requires explicit
rollback; recovery never automatically restores a database backup. A monitor
failure after retry has a distinct incomplete outcome.

Recovery accepts only the newest window, validates the whole file inventory
before effects and again after quiescence, and rechecks the live schema and old
runtime evidence before starting it. The tool refuses unscoped runtime-bearing
files, shared template drop-ins with active instances outside the restart order,
changes to protected templates, and candidate dependency changes. Explicit
config-only units receive managed drop-in updates and rollback without lifecycle
operations; after each reload their effective runtime references and identities
must match the expected state. The monitor service is in scope. Effective service dependency
propagation is checked before lifecycle effects and after reloads, including
indirect start and stop paths to untouched and config-only services; ordering alone is allowed.

The published release metadata contains `schema_max`; the current installed
`release-info` command does not. Schema compatibility must therefore be bound
to independently verified artifacts, not inferred from that CLI's output.

## Amendment: forward state migrations

A same-schema release retains the previous runtime as its rollback target. A
candidate below the live schema is refused. A forward migration requires the
old release schema to match the live schema and an explicitly named, distinct
`--rollback-release` whose verified metadata has the candidate schema. Its
assets, sealed runtime, identity and configuration receive the same preparation
and checks as the candidate. The plan digest binds both schema versions and the
rollback target.

The consistent database backup precedes the first start of any new-runtime
unit: workers can open and migrate the database before the controller starts.
Before starting, the live schema must still be the planned old schema; after
starting, it must be the target schema. Recovery stops the managed services,
reads the live schema under quiescence and selects the previous runtime for the
old schema or the prepared rollback release for the target schema. Any other
schema requires an owner decision and no service is started. Status shows the
currently applicable target; the journal records the choice. A database backup
is never restored automatically.

## Amendment: same-schema rollback is the previous release

Owner decision, 2026-11-21: a same-schema deployment rolls back to the release
installed before the window and to no other release. `--rollback-release` is
refused for it, and `apply` refuses a saved same-schema plan with any separate
rollback fields. The plan, the release manifest and recovery therefore name the
same rollback; before this, a named third release could be validated by the
manifest while recovery restored the previous runtime.

From schema 59 the previous release keeps its own verified, artifact-fixed
artifact-loss declaration capability, permitting or refusing. Rollback then
restores exactly the authority that was in effect before the window; it does
not append a declaration, but the owner may later make one through the explicit
acknowledged command. The plan binds that capability from the verified release
metadata, and `apply` rechecks it against the installed runtime before any
service stops. A forward migration still needs the distinct named rollback
release that refuses new declarations (ADR 0109). Refusing a permitting
previous release instead would block every later same-schema upgrade from it.

## Amendment: Hub config amendments on an accepted window

A hand edit of the installed Hub config invalidated the window manifest that an
external backup consumer pins, and made release recovery refuse the file as
an unrelated edit. Re-creating a manifest by hand fixed the pin but left
recovery refusing.

`reconfigure` records each change as a journaled amendment of the newest
`ACCEPTED` window, outside the plan seal: the plan, its digest and the window
manifest stay unchanged, so the window's evidence keeps its meaning. Each
amendment keeps its before and after bytes, a new manifest with the window's
wheels and state backup, and its own journal; it restarts and verifies the
services itself and restores the previous bytes on failure. Its `before` must
be the bytes the window and the earlier finished amendments installed, so the
chain cannot skip a change.

Only an allowlist of top-level keys may change, read by the Controller, sender
and monitor from the Hub config alone. A key that Claude workers read from
their own config would need both files changed in step, and trust,
credentials, project registration, security profiles, paths, worker counts and
the unit set are owner decisions or change what runs; all of these need a
release. Adding a key is a code change with its own review. A hand edit already
present is recorded with `--adopt` under the same checks, because otherwise
the first amendment on such an installation could never start.

Owner decision, 2026-11-23: a release rollback restores the config from before
the window and drops only the amendments made after it, listing them. The
caller acknowledges this with `--drop-amendments`, a flag the agent passes as
part of the procedure (ADR 0087, ADR 0092); it is not an owner step. Keeping
amendments across a rollback was rejected: the previous release never ran with
them, and their keys could be unknown to it. Only the newest window is
amended; after a rollback a change waits for the next accepted window.

## Deferred and status

Migration to symlinks and `stack.py` and the separate Codex server start
procedure are out of scope. Independent source review, release
provenance and installed acceptance remain prerequisites to calling the tool
supported. The CLI's `plan`
path validates the private input, discovers the old runtime, verifies release
metadata against the live database schema, prepares or verifies an inactive
runtime, validates candidate configurations and records a digest-bound private
plan. This path has offline fixture coverage; installed behavior is not yet
accepted. The deployment window, status and explicit recovery are implemented
and exercised with offline service-manager and runtime fixtures. They are not
installed or live acceptance evidence.
