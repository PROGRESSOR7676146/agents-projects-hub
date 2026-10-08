# Project status

Status: active alpha
Release: v0.7.0

This file describes repository capabilities only. It intentionally contains no
operator deployment inventory or live conversation evidence.

## Capability summary

Highest evidence for every row is automated offline tests at the stated
schema unless the row says otherwise; no row is live-accepted by this
repository. Live items are tracked in the
[stabilization backlog](../operations/STABILIZATION_PLAN.md#live-acceptance-backlog).
The prose sections below keep the detailed history until it moves into the
owning modules.

| Capability | Repository state | Live acceptance | Contract |
| --- | --- | --- | --- |
| Multiple Codex worker slots | Implemented | Pending (three projects) | [REQ-QUEUE-002](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0038](../decisions/0038-multiple-codex-worker-slots.md) |
| Claude Code CPA worker | Text-only default; protected file-tool permission boundary implemented offline (schema 38) | Pending; advisor isolation and full parity remain open | [REQ-AUTH-009/SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0052](../decisions/0052-protected-claude-file-permissions.md) |
| Managed Codex permission-profile continuity | External queue slice and immutable schema-39 snapshots; native offline adapter coverage | Pending; custody and local/advisor boundaries remain open | [REQ-SEC-001](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0055](../decisions/0055-managed-codex-profile-continuity.md) |
| Claude/Codex lead and advisor | Accepted plan; not implemented | — | [REQ-COLLAB-001](../product/IDENTITY_AND_INTERACTION.md), [ADR 0040](../decisions/0040-bounded-claude-codex-lead-advisor.md) |
| Participant evaluation and allocation | Accepted foundation; not implemented | — | [Requirements](../product/EVALUATION_AND_ALLOCATION.md), [ADR 0037](../decisions/0037-evidence-based-task-allocation.md) |
| Stop certainty and independent notices (schema 36) | Implemented; canonical checks and independent review at `d3be874` | Pending | [REQ-QUEUE-005/013](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0049](../decisions/0049-task-visibility-and-stop-certainty.md) |
| Final/progress send certainty (schema 43) | Source implementation under validation; exact publication gates pending | Pending | [REQ-QUEUE-005](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0060](../decisions/0060-final-and-progress-delivery-certainty.md) |
| Exact delivery control (schema 47) | Coordinated explicit consent and consumer implementation under validation; schema46 prerequisite published at `357409b` | Activation pending | [REQ-QUEUE-005](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0063](../decisions/0063-delivery-control-reconciliation.md) |
| Queue snapshots and accepted Codex activity (schema 37) | Implemented slice; focused offline validation, final canonical evidence and required review pending | Pending | [REQ-QUEUE-012](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0051](../decisions/0051-accepted-turn-activity-and-queue-notices.md) |
| Codex approvals observed before acceptance (schema 40) | Separate fenced observation slice under validation; accepted execution authority is unchanged | Pending | [REQ-QUEUE-012/013](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0051](../decisions/0051-accepted-turn-activity-and-queue-notices.md) |
| Claude process observations (schema 42) | Ordinary quiet notices implemented; offline candidate under publication validation | Pending | [REQ-QUEUE-012](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0059](../decisions/0059-claude-process-observation-notices.md) |
| Durable local-root blockers (schema 35) | Implemented | Pending | [REQ-WRITER-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0036](../decisions/0036-durable-local-root-blockers.md) |
| Exact Codex turn recovery (schema 34) | Implemented | Pending | [REQ-QUEUE-004](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0035](../decisions/0035-exact-terminal-turn-reconciliation.md) |
| Inbound Telegram materials (schema 33) | Implemented | Pending | [REQ-UX-009](../product/IDENTITY_AND_INTERACTION.md), [ADR 0032](../decisions/0032-durable-inbound-telegram-materials.md) |
| Context and quota telemetry | Implemented | Pending | [REQ-CMD-001](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0033](../decisions/0033-truthful-context-and-quota-telemetry.md) |
| Root exclusion, bounded concurrency, worktree lanes (schemas 31–32) | Implemented | Pending | [REQ-QUEUE-003](../product/DURABLE_QUEUE_AND_CONTROL.md), [ADR 0031](../decisions/0031-bounded-root-concurrency.md) |
| Saved Codex session connect | Implemented | Pending | [REQ-CMD-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0023](../decisions/0023-deterministic-session-connect.md) |
| CLI session adoption and replacement | Implemented | Pending | [REQ-WRITER-009](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0022](../decisions/0022-explicit-codex-session-adoption.md) |
| Explicit Codex provider routing | Implemented | Pending | [ADR 0028](../decisions/0028-explicit-codex-provider-routing.md) |
| Summary-free Codex `/local` and `/return` | Implemented | Required per deployed revision | [REQ-WRITER-006](../product/ACCOUNTS_CONTROL_AND_SECURITY.md), [ADR 0011](../decisions/0011-explicit-native-session-ownership-transfer.md) |
| Project-group provisioning | Implemented offline; deployment opt-in | Pending canary | [REQ-ONBOARD-006](../product/ONBOARDING_AND_ACCEPTANCE.md), [ADR 0024](../decisions/0024-user-authorized-project-group-provisioning.md) |
| Registered-project editing (schema 29) | Implemented offline | Pending canary | [REQ-PROJECT-EDIT-001](../product/ONBOARDING_AND_ACCEPTANCE.md), [ADR 0027](../decisions/0027-no-silent-session-rebind-on-project-relocation.md) |
| Scoped acceptance actor | Implemented | Live authorization pending | [AC-F-011](../product/ONBOARDING_AND_ACCEPTANCE.md), [ADR 0003](../decisions/0003-scoped-telegram-acceptance-actor.md) |
| Immutable releases and rollback rehearsal | Implemented offline | Deployment-owned | [REQ-OPS-010](../product/PERSISTENCE_AND_RECOVERY.md), [ADR 0012](../decisions/0012-verifiable-immutable-deployments.md) |
| Codex multi-auth account pool and rotation | Retired | Not applicable | [ADR 0047](../decisions/0047-retire-codex-multi-auth.md) |
| Off-machine recovery | Planned | Drill pending | [REQ-OPS-012](../product/PERSISTENCE_AND_RECOVERY.md) |

## Quality checkpoint

Multiple Codex worker slots are implemented behind external queue configuration;
offline admission, root exclusion, fairness and health checks are covered.
Deployment and live three-project acceptance remain pending. See
[REQ-QUEUE-002](../product/DURABLE_QUEUE_AND_CONTROL.md#implemented-queue-compatibility-and-local-provider-worker-isolation)
and [ADR 0038](../decisions/0038-multiple-codex-worker-slots.md).

Claude Code has a text-only external worker with native identity preparation,
bounded visible streaming and saved-result recovery. Offline tests cover the
invocation, failure and restart boundaries in
[ADR 0050](../decisions/0050-claude-native-invocation-evidence.md). Human approvals,
write-capable lead, isolated advisor, native local transfer, session connect and
live CPA/account acceptance remain pending under
[REQ-AUTH-009](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

Queue admission snapshots and accepted Codex turn activity are implemented in
schema 37 under [ADR 0051](../decisions/0051-accepted-turn-activity-and-queue-notices.md).
Offline evidence covers durable deduplication, binding changes, approval
resolution, passive deadlines and delivery certainty. The integrated revision
`3750ccfb0f8eb98333f9219f3a697d6328890d04` passed canonical and hosted validation
after exact-candidate independent reviews; active-work retry controls, approval
before native acceptance and wider provider visibility remain open. The
[notice-bound retry candidate](../decisions/0054-notice-bound-work-retry-reports.md)
now reports the same existing job offline; publication, independent review and
live acceptance remain pending. This does
not change the text-only Claude capability boundary.

Participant evaluation and resource-aware task allocation are an accepted
product foundation; implementation and acceptance remain pending. See
[the owning requirements](../product/EVALUATION_AND_ALLOCATION.md) and
[ADR 0037](../decisions/0037-evidence-based-task-allocation.md).
The initial [Claude/Codex milestone](../operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md)
is scoped to one lead, a read-only advisor and a minimal outcome journal;
automated scoring and parallel writers are deferred. Full provider integration,
role enforcement and live acceptance remain pending; see
[ADR 0040](../decisions/0040-bounded-claude-codex-lead-advisor.md).

Schema35 root-blocker admission is implemented offline; deployment/live
acceptance remain pending under
[REQ-WRITER-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md) and
[ADR 0036](../decisions/0036-durable-local-root-blockers.md).

Schema 34 Codex recovery and exact already-open local reconciliation are in
repository implementation, pending immutable deployment and live Telegram /
provider acceptance. Offline tests exercise exact terminal evidence,
inspection-first continuation, paused queue work and ownership checks; see
[REQ-QUEUE-004](../product/DURABLE_QUEUE_AND_CONTROL.md#implemented-queue-compatibility-and-local-provider-worker-isolation)
and [ADR 0035](../decisions/0035-exact-terminal-turn-reconciliation.md).

The integrated development baseline retains released schemas 26–30 unchanged,
adds root exclusion and bounded concurrency as schemas 31–32, and adds durable
inbound Telegram materials as schema 33. Project
relocation commits topic execution scopes with the registry/binding transition,
including crash recovery. Transient admission faults in dynamically onboarded
groups retain their Telegram offset and retry through idempotent queue admission.
Native Codex route/model continuity and Antigravity model/effort pinning remain
part of this baseline. Deployment and schema-compatible runtime rollback are
separate gates; existing schema-30/32 executables cannot open schema-33 state.

Inbound attachment/caption/album completeness and accurate Codex
context/quota-window telemetry are repository-complete. Deployment and live
acceptance remain separate; see the [roadmap](../ROADMAP.ru.md).

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


Antigravity native `/local` now pins the session model and effort with the same
argument builder used by productive turns. Existing effort suffixes are replaced
without duplication. Local CPA/direct profile isolation remains deployment-owned;
each deployment must prove migration, atomic settings persistence and a same-session
Telegram/TUI/Telegram round trip separately from these offline regressions.

Native Codex catalog discovery no longer requires the retired multi-auth helper.
The monitor reads `model/list`; isolated Controller refresh callbacks request
asynchronous discovery and retain last-good selectable models without provider RPC.
When a proxy returns a union catalog, the Codex selector admits only OpenAI
GPT/o-series identifiers, including through an explicitly configured route.
It sanitizes older cached snapshots on display and refreshes snapshots created
before this boundary; discovery failure cannot re-expose foreign models.
Codex `/local` now emits a native TUI attach to the configured shared Unix socket,
preserving the same persisted thread instead of opening a competing standalone writer.
Package F is repository-complete at schema 32. `max_parallel_roots` defaults to
one and transactionally bounds active Hub worker scopes across provider workers;
values up to 16 require external queue mode. Durable least-recently-granted
selection prevents a continuously polling live worker from starving another,
while a stale worker leaves consideration after two minutes. Lowering capacity
drains current turns without cancelling them. Expired execution becomes
root-local uncertainty: it still excludes its own canonical root but no longer
occupies a global slot or blocks an independent root. Cache-only status exposes
only capacity totals, bounded worker/agent/phase owners and an aggregate
uncertain-scope count.

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
fails locally on a dirty or non-`HEAD` publication, a missing or
mismatched external author-policy declaration, an unavailable GitHub repository
variable, or canonical validation failure. The hook binds Python imports to the
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

## Implemented

Passive recovery diagnostics now compare the running Hermes Hub-plugin import
source with the exact clean Hub release and live database schema. A mismatch
is optional-channel degradation in doctor, but blocks deployment acceptance.
Schema rollouts include the idle gateway as a database consumer without changing
its native providers. Bootstrap refuses an existing Hub installation and no
longer installs legacy multi-auth ordering. Both owners' recovery capsules must
be republished after topology changes; see the recovery operations guide.

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

- Numeric project/topic identity, canonical allowlisted roots, idempotent
  routing, persistent provider sessions, bounded visible context, and writer
  leases backed by versioned SQLite migrations.
- Additive durable provider-job, result, and Telegram-outbox schema with atomic
  idempotent enqueue, strict per-topic FIFO leases, canonical-root execution
  exclusion, conservative stale-job recovery, and a feature-gated embedded
  compatibility consumer. `dispatch_mode`
  defaults to `inline`; `queue_runtime` defaults to `embedded`.
- Isolated queue workers are available for locally managed Codex, OpenCode, and
  Antigravity behind `dispatch_mode: "queue"` and `queue_runtime: "external"`.
  `external_worker_agent_ids` selects each isolated provider independently and
  defaults to `codex`, preserving rollback and embedded compatibility for every
  other provider. Each worker owns only its adapter/process and SQLite execution,
  writes results and outbox rows, and neither reads Telegram credentials nor
  sends Telegram. `outbox_runtime` defaults to the stage-5 `controller` delivery
  path for safe rollback. When explicitly set to `external`, a standalone fair
  outbox sender owns every locally managed queue agent's
  Telegram identities and durable delivery retries; it has no provider adapter
  or RPC capability. The controller neither constructs adapters for isolated
  agents nor delivers their prepared rows. Direct-message provider services remain
  separate endpoints. Hermes remains externally managed
  and out of worker scope. Controller status/account commands never invoke a
  provider or account helper. No live queue cutover is implied by
  this repository change. Controller, direct-provider, worker, and sender
  processes handle `SIGTERM`/`SIGINT` as stop requests, use bounded Telegram
  polling and cleanup joins, release work when stop is observed before
  invocation, and preserve work past the final cooperative boundary for
  conservative ambiguity recovery. Managed-socket ownership guards remain part
  of the service/recovery stage.
- Additive SQLite runtime-health cache and bounded state APIs cover Controller,
  sender, monitor, and provider-worker identities. Wheel builds embed package
  version, exact Git SHA, build time, and a clean-tree assertion; each required
  process publishes that identity with its heartbeat. Cache-only `status`
  deterministically reports a converged, mixed, or unknown deployment without a
  provider/runtime probe. Monitoring emits one transition alert for a mixed or
  unknown revision episode and re-arms only after convergence. Existing
  heartbeat/error/provider-state classification and the single configured Hub
  Operations destination remain unchanged.
- A private mode-`0600` deployment manifest binds distinct active and rollback
  wheel digests and embedded clean-tree identities, the private configuration
  digest, the SQLite-consistent backup digest/schema, and the intended target
  schema. Its read-only gate re-inspects both wheels and rejects promotion or
  runtime rollback unless both immutable artifacts support the target schema;
  it never migrates state, starts services, or contacts a provider.
- The automated release dry-run creates its production-shaped schema-20 state,
  configuration, backup, manifest, unpacked immutable release directories, and
  activation pointer under one temporary root. It switches to the candidate,
  migrates to the candidate's target schema using candidate code, verifies the
  manifest, switches back, runs the rollback artifact against the retained target, and compares
  queued/outbox/indeterminate rows byte-for-value. It has no service, provider,
  Telegram, credential, or live-state capability.
- Telegram polling and durable-send failures are classified without raw
  exception text as bounded operation, network/API class, optional safe HTTP
  status/retry-after, consecutive-failure count, and last-success time.
  The first two consecutive failures remain visible without falsely degrading
  the component; the third degrades health and emits one edge for the whole
  episode. A successful request emits one recovery only for a degraded episode,
  clears it, and re-arms the threshold. Direct-provider pollers use the same
  threshold/recovery contract without becoming required deployment-health
  components.
  Advisory chat-action failures remain best-effort and do not block or repeat
  provider work.
- Central Telegram ingress with deterministic ordinary, Reply, mention, and
  quote routing; forwarded messages are passive durable context and bypass all
  command/stop/provider parsing. Non-target providers are not invoked merely to observe. An
  explicit Codex mention while another provider is active uses a satellite
  Codex session and does not silently change the active provider.
- Versioned provider-neutral Telegram interaction instructions now seed new
  Codex, OpenCode, Antigravity, Gemini-compatible, and Hermes sessions. Codex
  receives Contract v2 through native app-server `developerInstructions` on
  thread start and resume; its stable contract is no longer embedded in the
  user turn. Other providers retain the bounded prompt fallback until their
  native channel passes separate capability and behavioral acceptance. Existing
  sessions receive the full current contract once after rollout; its version is
  acknowledged only after a successful provider turn, then compact reminders
  avoid paying the full contract cost repeatedly. Both forms explicitly require
  one focused question before drafting when missing audience, facts, format, or
  language materially changes the requested deliverable. Provider-specific notes tune
  presentation without changing safety authority. Local `doctor` diagnostics
  list the acknowledged version and binding state for at most 100 current
  active/satellite provider sessions, identified by their Hub session ID;
  archived sessions and raw provider thread IDs are omitted. This provenance is
  informational and does not expand the ordinary mobile `/status`. Private-chat
  queue admission and external sender refresh use Telegram's native ephemeral
  `Thinking…` draft; project groups retain the bounded `typing` action because
  Bot API drafts are private-chat only. Receipt ticks remain Telegram-owned and
  are not imitated with reactions.
- Long provider results are split into ordered, independently valid Telegram
  HTML messages instead of being truncated. Queue-backed delivery persists each
  part and resumes at the first part without a recorded Telegram message ID;
  provider execution is never repeated for a delivery retry.
- Queue-backed project turns accept deliberate artifacts only from the exact
  per-job staging directory. Accepted files are copied into a private Hub-owned
  spool and bound to the durable outbox by size and SHA-256; the sender verifies
  the snapshot again immediately before upload and removes it only after Telegram
  acceptance. Valid files are bounded by aggregate bytes rather than an arbitrary
  attachment count. Shared stale files, symlinks, unsafe filenames, archives, and
  secret-like names are rejected with a bounded visible notice. Hub-owned
  direct-message and legacy inline turns use the same isolated staging,
  validation, and immutable spool boundary with immediate delivery. Hermes retains
  its independent native Gateway transport rather than sharing Hub credentials.
- Providers declared `managed_externally` retain their native admission path
  and are never enqueued into the local worker queue, preventing accepted jobs
  without an eligible consumer. All-external productive routes remain unclaimed
  by Hub, mixed routes admit only their locally managed target set, and model
  menus use cached/configured data without invoking the external runtime. Codex
  remains the locally managed primary provider. Startup detects nonterminal
  queue rows left by a prior ownership configuration and fails visibly until
  they are drained or explicitly reconciled.
- Optional separate Hub Telegram controller identity for project-group ingress,
  commands, callbacks, and menu ownership. It persists a distinct `hub` update
  offset, keeps Codex as the default productive provider, and does not replace
  provider response/outbox identities. The controller loader opens only the Hub
  token when configured, or only Codex's token for legacy ingress; omitted
  `hub_bot` configuration preserves the prior Codex controller behavior.
- Telegram token files are validated as a single credential rather than merely
  containing a colon, so labels or copied surrounding text fail preflight. The
  operational timer schedules its first run relative to activation and every
  later run relative to the monitored unit, including a post-cutover start.
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
- Automatic inter-agent handoff and unseen-dialogue injection are disabled at
  both routing and SQLite boundaries. Provider/model switches are deterministic
  local state changes. The bounded topic journal remains available only through
  the explicit advanced `/context [agent_id] [1..20]` request; it is intentionally
  absent from the compact Telegram command menu. User-forwarded messages retain
  their separate passive-quote semantics for the next productive turn.
- The scoped MTProto acceptance actor has fixed checks for deterministic
  commands, full model selection, provider connectivity, Reply provenance,
  passive forwarded quotes, rapid multi-message bursts, and bounded
  emergency-stop recovery. The stop check accepts either an active-turn stop
  acknowledgement or an explicit nonzero queued-job cancellation before it
  verifies a fresh provider response. A Codex-only Contract v2 scenario selects
  the exact aligned Codex identity and evaluates four delivered behaviours: a bounded
  short answer, focused clarification, ordered complex-task approach and
  recommendation, and exact artifact attachment. Repository tests cover the
  evaluator; each deployment still requires its own private live evidence. An
  aligned two-provider scenario verifies that a
  switch injects no history and `/context` retrieves only explicitly selected
  visible history. It accepts no arbitrary prompt from configuration
  and fails fast on the first failed scenario or when unrelated senders
  contaminate the dedicated canary topic.
- Its opt-in `p0_p1_live` scenario now replaces the deployment-local runner for
  seven P0/P1 checks. It proves provider-visible document/album bytes, FIFO
  admission during an active turn, durable cardinality across a controlled
  Controller/worker restart, the explicit over-20-MB notice, truthful response
  context/quota labels, and read-only status/account output. Private state and
  restart authority remain explicit deployment inputs; repository tests prove
  the actor logic, not a live Telegram or provider result.
- Hub-owned stop acknowledgements for affected work are persisted through the
  shared Telegram outbox and delivered by the Hub identity. Idle stop replies
  remain immediate. Release environments use the checked-in hash-locked
  runtime dependency export, whose exact derivation from `uv.lock` is enforced
  by the repository validation gate.
- Codex app-server, Hermes Gateway integration, OpenCode, and Antigravity
  adapters with isolated failure boundaries. Contract tests pin structured CLI
  output, safe argv/approval modes, app-server RPC shapes, Hermes hook fields,
  and Antigravity statusline cache safety; incompatible output fails only the
  owning adapter/worker.
- The Codex worker selects the official stdio fallback before `turn/start` when
  the shared app-server socket is absent or refuses connections. Transport transfer
  starts a new thread with bounded visible context instead of attempting to
  resume a thread still writer-locked by the shared server.
- Shared Codex sockets retain `on-request` approvals for their companion client.
  The isolated stdio fallback uses `never` inside `workspace-write` and
  explicitly declines any unexpected server approval request, preventing a
  headless turn from waiting forever on a companion that cannot reach it.
- Hub-owned Codex turns carry an approval-only transport marker. A compatible
  tlive companion still forwards their Allow/Deny requests, but suppresses
  duplicate prompt/completion cards and reply-to-continue. Interactive Codex
  sessions on the same shared socket retain the full Agent Session Remote UX.
- Compact `/status`, `/accounts`, cached and paginated `/model`, confirmed
  single-session `/new`, `/local`, and `/return` controls. Codex `/return` is an
  idempotent local lease transition with no provider invocation, summary,
  transcript copy, or session-ID change. OpenCode and Antigravity retain their
  prior bounded-summary return pending separate native-resume acceptance.
- Private last-known-good provider catalogs with bounded callback keys. The
  deterministic monitor refreshes stale Codex, OpenCode, and Antigravity
  catalogs every 12 hours without invoking a model; failed discovery preserves
  the last good snapshot and raises one edge-triggered warning. The monitor
  never executes an account helper; a Codex catalog cached from the retired
  multi-auth matrix is replaced by native `model/list` metadata on the next
  refresh.
- Provider-supplied OpenCode reset telemetry. The isolated OpenCode worker watches only runtime-log bytes
  appended after its owned process starts, recognizes the provider's exact
  usage-limit/reset phrase even when the CLI omits HTTP status, terminates a CLI
  that otherwise remains alive, and releases topic FIFO with a cached quota
  failure instead of waiting for the general turn timeout.
- Operational notifications are edge-triggered: unchanged deployment, catalog,
  and provider conditions are sent once and re-arm only after recovery. A
  configured Hub bot owns these service messages. Provider bot identities are
  never used for Hub-owned
  operational notifications, and an operations topic without `hub_bot` is
  rejected during configuration loading. Automatic Codex session context-size advice is
  disabled; compaction remains user initiated. Alert episodes left by the
  retired Codex account pool are released on the next notifying cycle.
  Doctor also checks a configured loopback Codex provider proxy without probing
  remote provider URLs or disclosing the configured endpoint; monitoring emits
  one alert per unreachable episode and re-arms after recovery.
- Durable masked Codex account snapshots for provider-free Controller status,
  plus private masked account hints and honest unknown-limit display for other
  providers. Cached quota and live worker availability remain separate signals:
  `/status` and `/accounts` surface a known provider/network failure in red even
  when a telemetry cache still reports unused quota; a newly started worker
  remains yellow/unknown until a provider turn proves availability. Stale quota
  values remain labeled as cached and cannot trigger low-quota alerts.
- Provider replies share one compact Telegram identity line: session and agent
  are not duplicated, model and effort use one label, and runtime implementation
  details are hidden. Available context and quota telemetry uses short follow-up
  lines with mobile-friendly reset timestamps; unavailable fields are omitted.
- Provider failure notices use the same durable Telegram outbox without being
  misclassified as successful model results. Antigravity consumes only a
  per-turn private diagnostic log, recognizes the provider's unsupported-network
  precondition without exposing raw logs, and reports the safe cause promptly;
  unknown post-invocation failures remain non-retryable and visibly uncertain.
  All generic uncertain notices state what happened, what Hub saved, and the next
  safe action.
- Declarative Telegram command-menu synchronization.
- Durable bounded Telegram burst collection keeps an unaddressed continuation
  with the first part's provider, including a satellite provider; socket-backed
  Codex same-turn steering, deterministic queued follow-up for runtimes without
  steering, and exact-utterance emergency stop with provider/process
  interruption are also implemented.
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
- Optional read-only Antigravity structured status/quota cache integration for
  compact `/status` and `/accounts`; private-file and freshness checks fail to
  unknown without invoking a model, and `doctor` reports each cache as fresh,
  stale, missing, malformed, oversized, or permission-unsafe.
- Recovery diagnostics accept the independently managed Hermes Gateway's fresh
  local heartbeat and bounded tlive status markers as liveness evidence while
  reporting supervisor state separately as active, confirmed inactive, or
  unavailable. A failed supervisor-bus probe is never labeled as an inactive
  unit, and independently healthy runtime evidence remains visible alongside
  it. Token-bearing tlive dashboard URLs are neither returned nor logged by the
  probe.
- Sandboxed Antigravity `accept-edits` mode; dangerous permission bypass is
  rejected.
- Official Codex login with a shared-socket preference and official stdio
  fallback; the `codex-multi-auth` integration is retired and its configuration
  keys are rejected ([ADR 0047](../decisions/0047-retire-codex-multi-auth.md)).
- Independent Hub, Hermes Gateway, and tlive diagnostics and monitoring.
- A clean-tree Hub-owned recovery capsule publishes a self-contained schema-33
  immutable-deployment triage guide, source revision, timestamp, and content
  hashes into a neutral local store for the independent Hermes channel. It
  carries no private deployment inventory and creates no service dependency.
- Canonical validation and CI audit package version, the newest changelog
  release, project-status release, and local `vX.Y.Z` tags for contradictions.
  Missing release tags are reported as non-mutating debt; Git SHA reported by
  immutable runtime artifacts remains the deployment identity.
- Privacy gate that rejects deployment identities, raw histories/session dumps,
  owner-specific paths, Telegram secrets/identifiers, and local runtime files.
  One external declaration can authorize only an exact public author-email span
  in a strictly structured, pinned-signature GitHub merge. The fingerprint rule
  alone may also be suppressed on a complete author-name or hosted source-owner
  span that exactly repeats the valid local origin owner; all surrounding
  metadata and unrelated rules remain scanned.
- Documentation validation inventories normative requirement IDs, protects
  numbered baseline sections by content hash, and checks local Markdown
  files/anchors repository-wide. The product index routes capability modules;
  the manifest records their current integrity inventory.

## Acceptance still required per deployment

- Summary-free same-session Codex return from ADR 0011 has automated coverage
  for active-work rejection, local-writer Telegram rejection, duplicate return,
  restart persistence, absence of a model call, and same-session continuation.
  Each deployed revision still requires its own Telegram → native CLI →
  Telegram acceptance. Other providers are outside this acceptance claim.
- Dedicated-user bounded Telegram baseline after deployment-local MTProto
  authorization. Repository checks define the safe scenarios; each deployment
  must still produce its own private live evidence.
- Natural or controlled Codex quota transition.
- Telegram privacy/admin policy, restart continuity, and reply provenance after
  any material provider or routing upgrade.

Live acceptance results belong in private operational records, not this public
repository.

## Planned recovery exercise

- The separate off-machine WSL recovery plan defines encrypted versioned
  application snapshots, periodic cold exports stored away from the physical
  source machine, exact recovery-set inventory, network/service isolation,
  preservation of all indeterminate work, and measurable cold-restore gates.
  Backup automation, WSL shutdown/export, and the first private timed drill were
  intentionally not executed during v0.7.0 repository preparation.

## Deferred

- Automatic Antigravity account rotation pending a stable supported headless
  pool interface.
- Provider-neutral Session Bridge.
- Full provider parity for semantic remote companions.

## Rejected

- Automatic approval or sandbox relaxation.
- Telegram-selected filesystem paths or silent project rebinding.
- TUI screen scraping and message-by-message CLI transcript mirroring.
