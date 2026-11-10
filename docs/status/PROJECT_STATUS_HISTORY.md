# Project status history

Earlier source and validation checkpoints were moved unchanged from
[project status](PROJECT_STATUS.md) on 2026-11-10. They record the state at
those checkpoints; current capabilities and open acceptance are tracked in
project status and the [stabilization plan](../operations/STABILIZATION_PLAN.md).

## Earlier quality checkpoints

Schemas 26–30 remain unchanged; schemas 31–33 add root/concurrency and materials.
Project relocation commits topic execution scopes with the registry/binding transition,
including crash recovery. Transient admission faults in dynamically onboarded
groups retain their Telegram offset and retry through idempotent queue admission.
Native Codex route/model continuity and Antigravity model/effort pinning remain
part of this baseline. Deployment and schema-compatible runtime rollback are
separate gates; existing schema-30/32 executables cannot open schema-33 state.

Context/quota telemetry is repository-complete with offline notification,
snapshot and display regressions; no new migration beyond schema 33. Behavior
is defined by [REQ-CMD-001..003](../product/ACCOUNTS_CONTROL_AND_SECURITY.md#compact-control-surface);
see [ADR 0033](../decisions/0033-truthful-context-and-quota-telemetry.md) for rationale.

Inbound materials are repository-complete at schema 33. Offline parser,
actual-provider-input, migration, album arrival, deduplication, forwarding,
native-image RPC and tamper regressions cover
[REQ-UX-009](../product/IDENTITY_AND_INTERACTION.md#telegram-interaction-contract)
and [REQ-QUEUE-010](../product/DURABLE_QUEUE_AND_CONTROL.md#implemented-queue-compatibility-and-local-provider-worker-isolation).
Deployment and live Telegram/provider acceptance remain separate; see
[ADR 0032](../decisions/0032-durable-inbound-telegram-materials.md).

Native Codex catalog discovery, union-catalog filtering and cached Controller
refresh are implemented under [REQ-CMD-002](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Discovery failure preserves the last good catalog; no retired account helper runs.
Codex `/local` now emits a native TUI attach to the configured shared Unix socket,
preserving the same persisted thread instead of opening a competing standalone writer.
Schema32 root capacity/fairness is implemented; its owning contract is
[REQ-QUEUE-003](../product/DURABLE_QUEUE_AND_CONTROL.md). Lower capacity drains
without cancellation; uncertainty retains root exclusion independently of global
capacity. Cache-only summaries remain bounded and contain no model calls.

Explicit local worktree binding now atomically moves an idle topic to the lane's
canonical scope. Bind and archive refuse pending, active, local-writer and
unresolved work. Before provider access the worker revalidates the exact derived
path, allowlist membership, Git worktree registration and Git top level, then
uses that lane as cwd. The same validated root is used for embedded execution,
staging, local resume preparation, and read-only Codex recovery. Retained
legacy project scopes (including null/empty fallbacks) are atomically reconciled
at Controller, worker, and standalone external-service startup,
using saved origin/checkpoint roots before registry fallback, including known
IDs whose current registry root conflicts. Saved root protection and uncertainty
are preserved; mismatched execution is refused without blocking unrelated roots.
Ambiguous saved evidence fails normalization atomically. Active lane scopes stay
distinct. Retained lanes fail closed in unsupported productive inline and managed
terminal paths, even after a configuration change. `/local` and non-Codex summary
return validate roots outside SQLite and recheck persisted identity inside the
writer transaction; invalid lanes or stale snapshots cannot transfer ownership.
External workers compare the already-canonical target against Git's registered
worktree paths without resolving unrelated entries, so a valid lane remains
usable when systemd `PrivateTmp` hides a different temporary worktree.
Managed inline terminal takeover also claims ownership with a checked snapshot
before provider preparation and process launch. Unconfirmed launch retains the
claim until explicit `/release`; process liveness cannot automatically return it.
Codex return stays model-free. Each provider worker still owns one SQLite connection and
one adapter/client/process lifecycle, so configured worker count is a second
parallelism bound. Offline capacity, fairness, contention, targeted-stop,
process-recovery, lane execution, migration and rollback tests cover this
checkpoint. Direct-message databases, unmanaged Hermes/native CLI processes,
deployment and live provider acceptance remain outside the claim. See
[ADR 0031](../decisions/0031-bounded-root-concurrency.md).

Schema 31 established one Hub-owned productive writer per canonical registered
root across Telegram topics and providers. Queue lease selection, local writer
transfer, and saved Codex-session adoption share the transactional execution
scope. See [ADR 0030](../decisions/0030-canonical-root-execution-exclusion.md).

Hub-owned execution now revalidates the cached canonical allowlisted Git root
before provider access or project staging. Both queue modes reject filesystem
drift as a terminal pre-execution failure with a durable, path-free notice and
no automatic retry. Inline/native execution and local-transfer preparation use
the same guard; real linked Git worktrees remain supported. Offline regressions
first reproduced invocation through a replaced root and now cover refusal in
all three local queue runtimes. See [ADR 0029](../decisions/0029-execution-time-root-validation.md).
That checkpoint used schema 25 and did not itself provide root/lane-wide
execution exclusion, protection against every filesystem race, or acceptance
of unmanaged CLI/Hermes execution.
Deployment and live continuity remain unverified for this change.

Queue-owned productive ingress now retains the Telegram offset when a transient
SQLite fault prevents session preparation or leaves enqueue disposition
uncertain. It stops the current batch, waits with a bounded backoff, and returns
the update through the existing idempotent queue admission; a committed job or
input membership is not duplicated after Controller recreation or offset-write
failure. Diagnostic-event failure cannot turn the original admission fault into
an acknowledgement. Deterministic terminal/ignored inputs keep their existing
disposition, and inline turns remain outside this retry boundary after provider
invocation may have begun. The ingress change adds no schema. Offline polling/SQLite tests cover
these boundaries; no live Telegram or deployment acceptance is claimed.

The [quality and stability review](../operations/QUALITY_AND_STABILITY_REVIEW.md)
records the initial findings. Milestone one of the
[reliability plan](../operations/RELIABILITY_PLAN.md) now consumes buffered Codex
turn events, deduplicates completed visible items, retains bounded partial text
in durable notices after handled failures, distinguishes caught preparation
failures, and respects Telegram cooldowns in both senders. These notices never
mark uncertain work successful or replay the provider. New offline regressions
cover both queue paths and restart during delivery cooldown.

Milestone two adds schema-22 execution identity and visible-item checkpoints for
both Codex queue paths. Expired invocations and handled post-acceptance failures
first recover a saved completed result or perform an exact-turn read-only lookup;
unconfirmed work retains an indeterminate notice and eligible saved partial text,
without productive replay. Abrupt-process and handled-disconnect tests cover
accepted/partial/completed boundaries. Milestone three bounds stdio reads and
propagates clean WebSocket closure without stranding its sender. Passive monitor
output now reports aggregate outcomes, recovery counts, delivery delay and queue
ages without provider access or task identity. Runtime rollback after the
progress-delivery migration requires an artifact that supports schema 24.
The read-only `indeterminate-audit` command now classifies retained uncertain work
and its notification state without outputting content, mutating state, or authorizing
provider replay. Schema 23 adds immutable, fixed-value operator resolutions for an
exact indeterminate job; they preserve the original status and error evidence, and
the audit reports resolved and unresolved work separately. Failure notices use a
consistent what happened / saved / next action structure. Privacy, formatting,
typing, documentation, history, and full test gates pass in the current repository
history. Live deployment evidence remains private and is not implied by this
repository checkpoint.

A versioned publication preflight can now install the repository's pre-push
hook into the shared Git common directory so it covers every local worktree. It
fails locally on a dirty or non-`HEAD` publication or canonical validation
failure. The hook binds Python imports to the
current worktree before validation so an editable environment from another
branch cannot supply stale scanner code. Hosted exact-SHA checks remain required
because a local hook is bypassable and cannot guarantee external runner or
network availability.

Passive reliability alerts now use lease and runnable-queue state for provider
work, following [REQ-OPS-004](../product/PERSISTENCE_AND_RECOVERY.md), alongside
delivery and uncertain-outcome signals. The evaluator is isolated from the broader alert module
and reads only aggregate SQLite telemetry. Uncertain-result notices give a
copyable explicit continuation request while preserving the no-replay boundary.
Schema 24 now backs a separate progress-delivery queue. In external-outbox mode,
completed visible Codex commentary can be delivered immediately and then at
most once per job every 120 seconds through the provider identity. Progress is
bounded, restart-safe, deduplicated, superseded after terminal job state, and
cannot complete or replay provider work. Final results retain delivery priority.
The queue implementation lives outside the already large state module.

Package A now closes SQLite connections whenever `HubState.open()` fails before
transferring ownership, when a backup cannot open its destination, and when a
pre-backup migration failure occurs after the original connection is closed.
Focused regressions exercise those resource/error boundaries without changing
schema 24 or migration ordering. CI and tag-release workflows now share one
reusable Python 3.11–3.13 canonical validator; publication depends on its full
matrix and alone has write permission. Structural tests prove the checked-in
wiring, while a hosted Actions execution remains separate evidence.
Review regressions also require backup handles to close before failed-output
cleanup, preserve a destination that was never opened, and reject skipped or
error-tolerant validation. A temporary Git fixture executes the release revision
guard against matching and mismatched HEAD/event/annotated-tag commits.
Read-only alert metadata lookups and execution-journal migration fixtures now
explicitly close their SQLite connections; the latter were independently
identified by Python 3.13 ResourceWarning allocation traces. These fixes retain
the existing alert output, transaction semantics, and schema.

## Earlier implementation checkpoints

Schema 30 prepares immutable Codex origins for bounded provider identifiers and
persists the inspected provider across the connect marker transaction. Existing
origins, activation boundaries and thread reservations remain unchanged. Runtime
rollback requires a schema-30-compatible artifact; restoring an old database is
not a runtime rollback procedure.

An explicit `codex_model_provider` now pins Codex start/resume and `/local`
commands to one locally configured route without replacing session identity.
Discovery remains limited to OpenAI plus that exact provider. Original provider
provenance and `/return`'s model-free lease semantics remain intact. Offline
old/new-origin round trips cover socket and stdio execution; deployment-local
Telegram/CLI acceptance remains separate. See
[ADR 0028](../decisions/0028-explicit-codex-provider-routing.md).

Saved Codex CLI and Codex VS Code/app sessions can be connected through
`/connect` in a registered project topic, the owner-only Hub private control plane, or a short-lived code
issued by local `session connect [CONFIG]`. All three entrances use one durable,
model-free workflow. Bounded app-server discovery exposes only safe labels for
the exact canonical project root. A positive Telegram marker message becomes
the activation boundary; its delivery receipt, immutable-origin attachment,
replacement archive, writer transfer, code consumption and result preparation
commit atomically. A separate `/return` is unnecessary. Unknown marker and
topic-creation outcomes are not retried blindly and preserve the previous
binding. Schema 26 adds workflow, candidate, option, outbox and one-time-code
state. See [ADR 0023](../decisions/0023-deterministic-session-connect.md).

Project/group onboarding is implemented offline behind explicit
`project_provisioning.enabled` configuration. The owner-only Hub wizard accepts
a display name, an opaque configured root choice and a safe direct-child project
ID. A separate pinned Telegram user-session worker creates the private forum,
snapshots all configured owners, preflights them plus the Hub and locally
managed provider bot identities, adds and
verifies the non-creator owners as administrators before inviting any bot,
checks for exact active bot membership before each invitation so owner-assisted
recovery survives resume, normalizes Telethon's self-reference to an explicit
creator identity for membership verification, blocks left/banned/mismatched
results, leaves `managed_externally` provider membership to its native/operator
boundary, grants the Hub minimum topic/invite
rights, prepares an empty Git root, updates the registry and records the
immutable numeric binding. Session files are private and protected by one
process lock; mutation RPCs have bounded deadlines and no client retry. The
  sender converges one durable bot/group command operation per cycle after
  final-result priority, with persisted per-bot Telegram cooldowns that cover
  newly created bindings. Schema 28 adds the owner snapshot,
recoverable blocked stage, lease heartbeat and durable command-scope work.
Unknown Telegram creation/configuration outcomes stop without automatic retry;
proven preflight/configuration blocks can be resumed explicitly. See
[ADR 0024](../decisions/0024-user-authorized-project-group-provisioning.md) and
[ADR 0025](../decisions/0025-provisioning-fencing-and-owner-membership.md).
No user session, group, project, credential, service or live canary has been
created by this repository change.

Owner-only editing of an existing registered project is implemented offline in
the private Hub `/projects` workflow. The owner first selects a project, then
chooses either a Hub-local display-name change or an opaque safe Git-root option,
reviews the impact and confirms. Display-name editing does not rename the
Telegram group. Relocation preserves immutable `project_id`, numeric group/topic
identity and both directories; it never copies or deletes files or provider
history. Attached provider sessions, non-Telegram writers, queued/running work,
pending delivery and unresolved outcomes block relocation. Schema 29 records the
durable workflow and opaque options. A serialized registry/SQLite commit rolls
back handled faults and completes a post-registry-write crash before Controller
admission resumes. Archived provider origins retain the old root. Automated
evidence is fictional and offline; no live Telegram edit canary or deployment is
claimed. See [ADR 0027](../decisions/0027-no-silent-session-rebind-on-project-relocation.md).

The administrative `session attach-codex` preview/apply interface remains
available for recovery. Schema 25 introduced immutable origins and the original
first-return boundary. External workers continue the exact thread through socket
or stdio; unsupported modes fail closed. Automated evidence uses fictional
provider and Telegram adapters. Live continuity and a production rollback
artifact for schema 33 remain deployment-specific; the current target is schema 33.

## Earlier validation and migration checkpoints

- The automated release dry-run creates its production-shaped schema-20 state,
  configuration, backup, manifest, unpacked immutable release directories, and
  activation pointer under one temporary root. It switches to the candidate,
  migrates to the candidate's target schema using candidate code, verifies the
  manifest, switches back, runs the rollback artifact against the retained target, and compares
  queued/outbox/indeterminate rows byte-for-value. It has no service, provider,
  Telegram, credential, or live-state capability.
- A reusable fictional subprocess fault matrix exercises Controller admission,
  durable SQLite jobs, isolated external workers, and standalone outbox
  delivery together. Parent tests terminate child actors after enqueue but
  before offset persistence, during provider invocation, and after Telegram
  acceptance but before delivery persistence. It proves redelivery
  idempotency, conservative recovery on both sides of `executing`, outbox-only
  retry, same-root provider exclusion with explicit uncertainty resolution,
  responsive cached Controller status,
  and distinct Hub/provider polling offsets without network, credentials, or
  live services.
- Schema version 21 bounds diagnostic `runtime_events` to 30 days and the newest
  10,000 rows, pruning atomically with each insertion and independently of
  current health, alert state, and provider work. Version 20 added bounded
  Telegram transport state to runtime-health snapshots; version 19 added
  immutable release identity; version 15 added durable per-message Telegram
  outbox parts; and version 14 repairs early version-13 deployments that had
  durable input membership but had not yet created stop-request and
  turn-absorption tables. Upgrades create a private SQLite-consistent backup
  first. The full migration and `user_version` update share one immediate
  transaction, so a fault rolls back in place without overwriting concurrent
  commits from an older backup. A temporary production-shaped rehearsal covers
  schema 20→21, concurrent access, injected DDL failure, backup rollback,
  queued/outbox rows, and preserved indeterminate work.
