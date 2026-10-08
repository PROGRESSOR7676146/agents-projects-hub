# Stabilization plan

Status: active; stages 0–2 and 3b done, stage 3 in progress
Date: 2026-09-27; updated 2026-10-04
Owner: repository owner (decisions and merges); lead development agent integrates
Last verified integrated repository revision:
`3750ccfb0f8eb98333f9219f3a697d6328890d04` for stop, native Claude and schema-37
visibility (canonical and hosted checks, after independent candidate reviews).
The schema-38 protected file-tool candidate is implemented offline; follow
[ADR 0052](../decisions/0052-protected-claude-file-permissions.md) and the
[candidate runbook](CLAUDE_FILE_PERMISSIONS.md). Publication/review gates and
separately authorized native/Telegram acceptance remain open. Authority-isolation
candidate `6a848c7a326b35e4c3ea552339e8c5c3d7beba53` has passed canonical,
independent review and hosted checks but remains unmerged and undeployed.
Actual authority custody is still open. Assess the existing narrow boundary and
actual launch/service exposure first; a dedicated VM remains a reserve option,
not a selected prerequisite. The revised preparation decision is in
[ADR 0053](../decisions/0053-claude-custody-reference-deployment.md) and the
[custody preparation runbook](CLAUDE_CUSTODY.md).
Next trigger: the native human approval boundary in the
[continuation plan](NEXT_DEVELOPMENT_SESSION.md); continue the separately
authorized live backlog and ADR 0048 work.
Deployment identity and private acceptance records remain outside this plan.

## Current source integration checkpoint

The goal integration candidate at clean
`98c78fcfc40a3fba79a3cdecc2db8b609dabb78a` passed canonical validation
(2,035 tests in 185 modules, zero typing errors, privacy/history and static
gates) and all seven exact-head hosted checks. Its source includes notification
conservation/backpressure, completion-safe connection retirement, RPC deadlines,
schema-41 saved-task retry, managed-profile continuity, preacceptance observations,
Claude configured choices/exact model continuation and explicit unsupported
native-transfer refusal. This supersedes earlier pending-publication statements
below for those integrated source slices; opposite-runtime/owner integration
review, owner main merge and separately authorized live acceptance remain open.

The stacked provider-neutral namespace extraction at clean
`fcc755fe4d70ddbff6b544c2a1a7b09d4b27e0ac` passed canonical validation
(2,042 tests in 187 modules) and seven hosted checks. The bounded sealed-material
primitive at clean `cd8b6047177b92296df78f2dc5c1ef80c2c83490` passed canonical
validation (2,051 tests in 189 modules); required independent review and its
hosted checks remain separate. Both are offline foundations, with no productive
advisor, role workflow or installed custody claim. ADRs 0056 and 0057 belong to
those independent candidate branches; they are not part of this integration base.

The next bounded recovery follow-up gives a typed local response-budget expiry
the existing saved-task retry only at the exact pre-submission preparation
boundary. Generic/profile/post-submission errors and contradictory execution
evidence remain excluded; no new replay, approval, schema or lifecycle ownership
is introduced. Source owner: Hub maintainer, lane
`fix/codex-preparation-deadline-retry`, base `98c78fc`. After publication/review,
integrate and revalidate the combined revision before the two-worker canary.
Inspect tracked, staged and untracked lane state before any post-merge cleanup.

Full authority custody, broader provider progress, Claude native transfer and
saved-session connection, subscription/no-paid-fallback acceptance, role/review
workflow and outcome journal, three-project/restart acceptance, Hermes incident
and update plane, off-machine restore drill and release 0.8 remain open. Restart
alone closes neither the repeated notification incident nor payload recovery.

## Read-only outcome projection candidate

Current state: bounded offline implementation in lane
`feat/outcome-journal-projection`, based on source `93577e5`. Clean candidate
`dcc650794719a1be6c4143766bdb007265352471` passed the local canonical publication
gate on Python 3.11 (2,085 tests in 189 modules, zero typing errors, and passed
privacy/history gates). Astra and Claude Opus independently reviewed the runtime
at `1164770`; Opus found a supported-Python-3.13 test-gate failure. The `dcc6507`
test/documentation follow-up fixes it and received a separate Opus review with
no blocker/high/medium finding. All seven hosted checks at exact head `dcc6507`
passed, including the Python 3.11/3.12/3.13 matrix and required namespace job.
This remains repository evidence. The
[local diagnostic](OUTCOME_JOURNAL.md) projects one saved job without a migration,
provider invocation or new writer/decision authority. Requested/stored/observed
model provenance, result/delivery, direct lineage and source-bound time intervals
remain distinct. Acceptance, native durations and per-job usage stay unknown
where the existing records cannot establish them. This does not close
[REQ-EVAL-010](../product/EVALUATION_AND_ALLOCATION.md).
Source owner: Hub maintainer; next trigger: integrate the reviewed foundations,
then separately design authenticated immutable owner decisions and corrections.
Inspect tracked, staged and untracked lane state before any post-merge cleanup.
Closure remains open alongside the role/review workflow; there is no deployed
advisor, acceptance journal or accounting claim.

## Advisor foundations integration candidate

Current state: published in lane `feat/advisor-foundations-integration`,
base `dcc6507`, preserving the namespace/capsule histories at `fcc755f` and
`cd8b604`. Source owner: Hub maintainer, sole integration writer. Clean candidate
`1c70d5639b59cfa97829bb56deaf3f95f577cb42` passed the commit and canonical
publication gates (2,149 tests in 195 modules, zero typing errors, static and
privacy/history checks), independent exact-head Astra and Claude Opus reviews,
and all seven hosted checks including Python 3.11/3.12/3.13 and strict namespaces.
The strict affected corpus passed 123 tests with no namespace skips; its sole
skip was Python 3.11's absent memfd wrapper. The later Python 3.12 affected
subset passed 33 tests.

The neutral core carries the private-alias/parser/runtime protections for every
access profile; private validation descriptors never join inherited launch pins.
Independent review found that cold casefold lookups can preserve requested
spelling. The corrected shared pin owner therefore requires supported
case-sensitive filesystem evidence at every parent and terminal directory,
including final walks. Unknown/overlay ancestors, unreadable directories,
unavailable metadata and unsupported ABI refuse. Refusing ioctl sentinels reject
success without written evidence; exact inode-targeted capsule regressions and
FD allocation tracking preserve cleanup evidence without closing tested resources.
These are kernel-isolation and scripted metadata tests, not a real casefold/cache
or XFS acceptance witness. Bare/separate Git directories remain the trusted
caller's material-authorization responsibility. See ADRs 0056 and 0057.

Next trigger: native compatibility with an isolated fake inference endpoint,
then the authorized-material/workflow binding and bounded inference transport.
Closure remains open: these primitives enable no productive advisor, role
handover, owner assessment or installed custody. Private networking blocks the
existing loopback route. Owner main merge and deployment remain separate.
Inspect tracked, staged and untracked lane state before any post-merge cleanup.

## Offline native Claude transport follow-up

Current state: published source candidate `535c0b8` in lane
`test/claude-native-transport-corpus`, based on clean `1c70d56`; canonical
publication passed with 2,175 tests and Pyright 0. Exact-candidate Claude Opus
review has no remaining mandatory findings; Astra reviewed the production
follow-up. All seven hosted checks passed at exact head `535c0b8`, including the
Python 3.11/3.12/3.13 matrix and namespaces. Source owner: Hub maintainer.
The optional corpus uses an explicitly supplied native CLI with
synthetic credentials and an HTTP fixture inside private network/PID/IPC
namespaces; host files and loopback have positive and negative controls.

The four bearer/API-key × SSE-success/HTTP-529 cases exposed two compatibility
fixes: safe mode leaves optional built-in mods enabled, so both settings builders
now explicitly disable the known optional IDs; native API rejection uses the
success result variant with an error flag and explicit HTTP status. The strict
plugin guard remains unchanged, required security policy mods remain untouched,
and incomplete/contradictory HTTP-error evidence remains indeterminate. Verified
rejection does not establish an absence of earlier effects or authorize replay.
The process fixture additionally kills its owned group before reaping an exited
leader, and refuses special-file executable sources before open. The final
corpus inherits production start argv and checks its exact fictional session
identity. Strict native execution pins local binary/version evidence and requires
the exact 529 envelope, zero visible assistant messages on rejection and fully
validated host diagnostics. All four cases passed with CLI 2.1.285; additional
retry/output bounds belong only to the fixture.

Next trigger: confirm exact-revision hosted checks, then the bounded isolated
transport below. The
[testing guide](../testing/README.md#optional-offline-native-claude-transport)
owns the procedure and evidence limits. This does not close subscription routing,
CPA/no-paid-fallback, human file approvals, local transfer or Telegram acceptance.

## Bounded review pipe primitives

Current state: clean offline candidate `dd43ae6` in lane
`feat/advisor-inference-bridge`, based on `535c0b8`. Source owner: Hub maintainer.
The mandatory commit and canonical publication gates passed 2,204 tests in 198
modules with zero typing errors and privacy/history checks. Exact-candidate
Astra and Claude Opus reviews have no remaining mandatory findings; all seven
hosted checks passed at exact head `dd43ae6`. Early capsule-type/UUID
findings and Opus's exception-chain/request-byte findings are fixed with
negative regressions. See
[ADR 0058](../decisions/0058-bounded-review-pipe-primitives.md) for the selected
ownership and evidence boundary.

The published clean candidate `e563dd4ca51b5d4884227681782d4a7fce275b2e`
in lane `feat/advisor-pipe-sequencing` adds directional wire ordering and finite
partial-write accounting on `dd43ae6`. Commit/canonical gates passed 2,241 tests
in 200 modules, types and privacy/history; Astra and actual Claude Opus 5.5/high
source reviews have no mandatory findings. All seven exact-head hosted checks
passed, including coverage. The process-wide FD-count failure at its earlier
candidate has unknown allocation/close attribution; the replacement fixture
tracks identity-bound allocations without taking cleanup authority.
It keeps distinct request observation, callback consumption, buffered bytes,
caller-reported writes and native completion. Graceful wire cancellation drains
and discards in-flight stdout; buffer abort requires channel closure after a
partial frame. It introduces no physical I/O or runtime switch.
Source owner: Hub maintainer.

The separate owned nonblocking I/O/private namespace witness is published in
PR #154 at `cc976f6828554104b7326770f1e3a67b6e2de6ef`, based on the
sequencing prerequisite. Canonical publication passed 2,264 tests in 203 modules,
types and privacy/history; exact GPT Astra and actual Claude Opus 5.5/high reviews
found no mandatory findings. All seven exact-head hosted checks passed. These
fictional peer/HTTP and kernel namespace fixtures do not establish productive
advisor integration or a real upstream route.

Next trigger: owner integration of these separate lanes and native request
validation before productive wiring.
Closure remains open: no productive advisor, real upstream, durable role/material
authorization, native body validator, worker wiring, deployment or live acceptance
is enabled. The original project, host authority sockets and credentials remain
outside this proposed child boundary. Inspect all lane state before post-merge
cleanup; main merge and deployment remain owner actions.

## Claude process observations

Current state: source published in PR #155 at clean
`a542b2054250dcd7e60ad7df0969ca7462e4054b`, based on prerequisite
`e563dd4ca51b5d4884227681782d4a7fce275b2e`. Source owner: Hub maintainer.
[ADR 0059](../decisions/0059-claude-process-observation-notices.md) records the
separate process/permission evidence boundary. Focused actual-process, SQLite
and migration fixtures passed. Canonical publication passed 2,277 tests in
204 modules, full Pyright and privacy/history; exact clean GPT Astra and actual
Claude Opus 5.5/high reviews found no remaining blockers. All seven hosted checks
passed at that exact head. These are source/offline and hosted checks.

Next trigger: owner integration and separately authorized native
and Telegram acceptance alongside the remaining Claude parity work. This lane
is bounded to optional passive ordinary quiet notices; productive advisor
wiring, tool/build activity, local transfer, saved-session connection and billing
route acceptance remain open. Root is the sole implementation writer. The
worktree owner is Hub maintainer; its purpose is schema-42 observation and
worker/sender integration, with base `feat/advisor-pipe-sequencing`. Post-merge,
inspect tracked, staged and untracked state before worktree cleanup. Closure is
open; no deployed or live acceptance is claimed.

## Final/progress delivery certainty prerequisite

Status: schema43 source implementation under validation. Base verified revision:
`a542b2054250dcd7e60ad7df0969ca7462e4054b`; exact candidate publication remains
pending. Root is the sole writer; Hub maintainer owns this worktree. Its purpose
is durable send fencing, shared final-part policy, provenance and preservation
of late native proof. Base branch: `feat/claude-process-observations`. After owner
integration, inspect tracked/staged/untracked work before any cleanup.

The [owning contract](../product/PERSISTENCE_AND_RECOVERY.md) and
[ADR 0060](../decisions/0060-final-and-progress-delivery-certainty.md) define the
change. Next trigger: exact-candidate source reviews, canonical/privacy and
hosted gates, then the bounded owner-assessment slice. Deployment, Telegram
acceptance and unknown-delivery reconciliation remain separate; no provider
replay, writer change, service change or assessment command is enabled here.
Closure: open until exact source publication gates pass; live backlog stays open.

## Planned final-response mode indicators

Owner-requested follow-up (2026-10-07), implementation pending: extend the compact
final-response identity/telemetry footer with important active modes, initially
goal execution (`/goal`) and fast/service-tier selection (`/fast`), and other
supported modes that materially affect the owner's understanding of the turn.
Keep the footer concise; use authoritative state bound to the exact completed
turn, rather than interpreting prompt text or assuming that a requested mode was
enabled. Distinguish active, paused and terminal goal states; show fast mode only
when its actual provider setting is observable. Missing or unsupported evidence
must not become an enabled-mode claim. Reading/formatting this metadata must not
invoke a provider or change modes. Preserve the existing session, agent,
model/effort, context and quota information. Before implementation, define the
owning display contract and verify per-provider metadata, retries, provider/model
switches and missing/stale state. Source owner: Hub maintainer; next trigger:
the task-visibility follow-up after the current authority-isolation fix. This
plan item neither enables these modes nor changes their resource policy.

## Why

A read-only audit on 2026-09-27 found strong design and safety invariants but
weak enforcement: a branch carried three commits on a red suite; the last
fifteen pull requests merged without any review; `service.py` and
`state.py` kept growing after the refactoring plan closed, with no bounded
exception recorded; 31 production handlers swallowed exceptions silently and
the package had no logging; CI validated freshly resolved dependencies instead
of the lock; README still announced v0.5; and repository-complete features
accumulated without live acceptance, contrary to product principle 10.

## Owner decisions (2026-09-27)

- The Claude CPA worker scaffold is fixed, reviewed and merged as a scaffold.
  The owner later withdrew the provider-expansion gate (ADR 0045): Claude work
  toward Codex parity continues, and the backlog below is tracked debt, not a
  gate.
- Large or lifecycle/security pull requests receive an independent review by
  an agent that did not write them, recorded in the pull request. Only the
  owner merges into `main`.
- Hotspot growth is enforced by an automated ratchet, not a report.
- Diagnostics record exception class and a static site, never exception text.

## Stages

| Stage | Scope | State | Evidence |
| --- | --- | --- | --- |
| 0 | Fix the lifecycle test broken since `2d24e82`; review the Codex branch | Done: #80 | Canonical gate at `6ce927e`; review findings 1, 3, 4 fixed |
| 1.1 | Parallel isolated test modules; working focused selectors; pre-commit gate | Done: #81 | ADR 0041, ADR 0042; suite ~45 s instead of ~340 s |
| 1.2 | Bounded diagnostics for survived failures; Ruff S110/S112 | Done: #83 | ADR 0043 |
| 1.3 | CI from `uv.lock`; non-required coverage report | Done: #82 | Hosted run on 3.11–3.13 and coverage job green |
| 1.4 | README version checked by the release metadata audit | Done: #82 | Audit test |
| 2a | `CLAUDE.md`; review/merge rules; risk register; ADR 0016 tombstone; this plan | Done: #84 | — |
| 2b | Hotspot growth ratchet in every validation profile; rules 12 and 14 amended | Done: #85 | ADR 0044; 21 recorded hotspots |
| 2c | Capability summary table in project status; closure sections for six completed plans | Done: #86 | Prose still to move into owning modules |
| 3 | Extract `_handle_update`, `load_hub_config`, `cli.main`; remove dead code; tests for weak modules; injected clocks in timing tests | In progress: read-only `status`, `doctor` and monitor (#97); `cli.main` split into command handlers (#99); `run_monitor_once` and `evaluate_operational_alerts` below the hotspot line (#100); onboarding lease test on an injected clock (#101); `load_hub_config` split into section parsers (#103) | Dispatcher coverage 316 → 359 of 421 (#89) |
| 3b | Retire the multi-auth integration (reliability package B): migration contract, retired keys rejected before helper access, superseded ADRs | Done: #100 | ADR 0047; no schema change; host leftovers are a separate authorized task |
| 4 | Release 0.8.0; branch and worktree hygiene | Planned; each action needs owner approval | — |

## Live-acceptance backlog

Each item needs deployment-local evidence at an exact clean revision, recorded
privately (see [live canary](LIVE_CANARY.md)). Repository tests are not
acceptance.

### Prerequisites

- **Acceptance actor (AC-F-011): available, currently an owner.** The dedicated
  acceptance user already exists and has run the bounded baseline before; its
  configuration, session and pinned identity are private deployment state
  ([testing guide](../testing/README.md#dedicated-acceptance-user)). The account
  is now also a configured owner, which configuration validation forbids for a
  scoped actor, so it runs with owner authority instead of one canary topic.
  Owner decision 2026-09-27: keep it for now and later restore a scoped,
  non-owner actor on a separate account.
- **First baseline at `ea5af70` (2026-09-27): 14 of 15 checks passed.** Routing,
  Reply, forwarded quote, burst, artifact delivery, context isolation and the
  Codex interaction contract passed. `stop_route` failed because the actor
  depends on check order: `context_contract` changes the active agent, and the
  emergency stop correctly targets the active agent, not the mentioned provider
  running the test turn. The actor must select its stop target explicitly.
- **Outsider account: available on request.** As of 2026-09-27 the owner also
  has a second Telegram account that belongs to no project group and is in no
  allowlist. It enables live negative checks that the scoped actor cannot
  perform: direct messages and commands to the Hub and provider bots, stale or
  foreign callbacks and one-time `/connect` codes, and attempts to reach
  project data without authorization. Each must fail closed with no provider
  invocation, no state change and no disclosure (REQ-SEC-006, AC-F-007,
  AC-NF-001). The first pass is an owner-driven checklist; automation would
  need a separate, reviewed actor mode. No identifier of either account
  belongs in the repository.
- **Deployment of the exact merged revision** through the immutable release
  procedure with a schema-compatible rollback artifact, under the
  [live canary](LIVE_CANARY.md) stop conditions. Each run needs the owner's
  authorization and presence.
  2026-09-28, on the owner's authorization: `adb8b37` (#90–#94, schema 35)
  replaced `ea5af70` with an empty queue. Evidence level: release identity. The
  manifest binds both wheels, the configuration and a consistent backup, and
  the synthetic rollout and rollback passed. Every long-running component
  (controller, sender, provisioner, monitor and the three provider workers)
  reports the clean revision, and one monitor cycle through its unit reported
  no alerts.
  2026-09-29, on the owner's authorization: `46d6e7a` (#96–#104, schema 35)
  replaced `adb8b37`, with the same manifest, backup and rehearsal evidence.
  The live units received the daemon-socket binding of #104. On 2026-09-30 the
  Hermes Hub plugin moved to the same release; `doctor` passed every check,
  including plugin compatibility, and all seven required components report
  the clean revision. Evidence level: release identity; live E2E pending.
- **Live E2E at `adb8b37` (2026-09-28, owner-authorized).** The actor
  baseline passed 15 of 15 checks, including the topic-wide `stop_route` that
  failed at `ea5af70`. `p0_p1_live` passed 5 of 6: caption-only document,
  album, FIFO admission during an active turn, exactly-once recovery across a
  Controller and Codex-worker restart, and the explicit 20 MB notice. The
  context and quota label check failed: the live Codex response carried a
  numeric context remainder but no quota window. The deployed Codex route uses
  a custom model provider, whose app-server supplies no rate-limit windows, and
  the Hub omits unknown windows (REQ-CMD-001). `/status` has no Codex quota
  either: multi-auth is not configured on the deployment, so no cached pool
  quota exists to show. The runner stops at the first failure, so the
  read-only status and account check did not run. The queue, the outbox and
  pending stops were empty before and after both runs.
- **Live E2E at `f764ab8` (2026-10-01, owner-authorized): `p0_p1_live` 7 of
  7.** The quota label now passes (#102), and so does the read-only `/status`
  and `/accounts` check (#109 replaced its stale expectation of a Codex
  account section, impossible since ADR 0047). An earlier run at `a0b27ff`
  passed 6 of 7 on that expectation; a rerun the same day timed out on a
  queue wait behind unrelated work (see silent queue waits below). The queue,
  outbox and pending stops were empty before and after.
- **Actor coverage.** The actor automates status, accounts, model menu,
  provider ping, Reply/forward/burst/stop routing, artifact delivery, the
  context contract and the restart-authorized `p0_p1_live` scenario. `/local`
  and `/return`, `/connect`, provisioning, project editing and the recovery
  drill remain owner-driven scenarios.

| Capability | Requirement | Scenario |
| --- | --- | --- |
| Routing baseline: ordinary, mention, Reply, forward, burst, stop | AC-F-002, AC-F-011 | Acceptance actor bounded baseline |
| Inbound materials and context/quota labels | REQ-UX-009, REQ-QUEUE-010, REQ-CMD-001 | Acceptance actor `p0_p1_live` in a maintenance window |
| Restart continuity and exactly-once processing | AC-F-005, AC-F-010 | Controlled restart during queued and active work |
| Summary-free Codex `/local` → `/return` | REQ-WRITER-006, REQ-WRITER-007 | Telegram → native CLI → Telegram on the same thread |
| Managed Codex profile continuity and custody (schema 39) | REQ-SEC-001, ADR 0055 | Exact start/resume/restart selection, approvals, negative project/service-data access; local/advisor routes remain unsupported pending their own boundary evidence |
| Saved-session `/connect` | REQ-CMD-008, REQ-WRITER-012, AC-F-013 | Topic, Hub-private and local-code entry paths |
| Accepted-turn activity and queue snapshots (schema 37) | REQ-QUEUE-012, REQ-QUEUE-013 | Queue blocker, long tool, approval resolution, restart and ambiguous delivery at the exact deployed revision |
| Preacceptance Codex approval observations (schema 40) | REQ-QUEUE-012, REQ-QUEUE-013 | Human wait before native acknowledgement, exact promotion, worker-epoch restart and preserved unknown sends |
| Durable root blockers (schema 35) | REQ-WRITER-008 | Blocked input, held job, owner decision |
| Exact Codex turn recovery (schema 34) | REQ-QUEUE-004 | Uncertain turn, read-only proof, continuation |
| Root concurrency, worktree lanes, Codex slots | REQ-QUEUE-002, REQ-QUEUE-003 | Three projects on independent roots |
| Codex preparation notification conservation | REQ-QUEUE-004, REQ-QUEUE-012 | Keep one exact turn active on root A while root B starts or resumes; preserve both finals, early/late human approvals, context and account quota observations, with no additional invocation or unrelated-worker restart |
| Project-group provisioning | REQ-ONBOARD-003, REQ-ONBOARD-006 | New project canary |
| Registered-project editing | REQ-PROJECT-EDIT-001..004 | Rename and relocation canary |
| Machine-loss recovery drill | REQ-OPS-012 | Private cold-restore drill |
| Unauthorized access and disclosure | REQ-SEC-006, AC-F-007, AC-NF-001 | Outsider-account negative checklist |

## Follow-ups found during execution

- Codex preparation notification overflow: the source filter is present at
  `5a1b12d608873b4660b758f43d0e9a5dac37d404`. The combined offline regression
  uses two independent worker connections and roots, injects 3,600 foreign events
  during the second worker's initialization, preparation and turn submission,
  and checks both durable completions/outboxes, approval resolution and telemetry.
  Start and exact resume are separate cases. A control case reproduces the
  1,024-entry overflow under the older unfiltered retention rule. Next trigger:
  complete exact-revision publication/review, then separately authorize deployed
  conservation acceptance and bounded native event-source attribution. The deployed
  notification source remains unverified; offline scripts do not close live debt.
- Native notification attribution has an optional offline direct-socket fixture
  in `tests/test_codex_native_notification_origin.py`: two native connections,
  a deterministic Responses stream with more than 1,024 notifications, exact
  completion and a subscribed positive control. It uses no real login, model
  endpoint or deployment socket. Fresh observers and retained subscriptions are
  measured separately. Native subscriptions survive starting another thread on
  the same connection; worker-client reuse before strict completed-connection
  retirement, or without exact completion proof, permits that state. This is a
  demonstrated mechanism, not attribution of a particular deployment failure.
  Keep the foreign-notification filter independently of subscription cleanup.
  Transport bounds and completed-connection retirement are described below;
  their repository evidence does not close installed-source attribution. Publication/review and coordinated
  live acceptance remain separate gates.
- The Codex daemon moved its shared socket into `/tmp/codex-daemon-UID`, which
  the Hub units' `PrivateTmp` hid (found 2026-09-29). Since about 2026-09-27
  the Codex worker ran on the stdio fallback, without companion approvals, and
  the monitor could not refresh the Codex model catalog. Fixed in the unit
  templates by binding that directory (#104) and applied to the live units
  with the `46d6e7a` deployment; the catalog refreshed the same day.
- Reliability plan packages B–E: owner decision 2026-09-27 — B, retiring
  multi-auth, moves into this plan as stage 3b; C, D and E are deferred and
  stay recorded in the reliability plan.
- Claude failure classification and native invocation evidence (PR #112):
  repository implementation is present under
  [ADR 0050](../decisions/0050-claude-native-invocation-evidence.md), with candidate
  `c5e130c` passing canonical publication checks (1,375 tests, 128 modules).
  Hosted checks and independent review passed at that exact candidate; it is
  merged in the integrated revision above, whose canonical checks cover
  1,488 tests in 135 modules. This is source evidence, not live acceptance.
  Human approval hosting, tools, advisor isolation, native local transfer and
  CPA/account live acceptance remain separate parity work.
- `codex-worker@1` / `claude-worker@1` duplicate slot 1 of `worker@codex` /
  `worker@claude` (PR #80 review, item 5).
- Slot identity format and bounds are duplicated in four modules (PR #80 review, items 7–8).
- Real-clock lease tests fail when the host suspends (R-018).
- Emergency stop: R-021 closed on 2026-09-28. The result and failure commits
  are the last stop check, so a stop recorded after the worker's final check
  cancels the job instead of colliding with its outbox row, and a stop
  completes however its covered work ends (ADR 0046).
- Parallel test runner (Codex review of #93, P3, fails closed): a module or
  class fixture that runs twice in one module run can make a set-up skip also
  count tests that already ran, so the gate fails although discovery passes.
- Product principle 10 and the capability matrix contradicted the Claude
  scaffold; resolved by withdrawing the gate (ADR 0045).
- An analysis of the Claude integration path to Codex parity follows stage 3.
- Response quota labels on a custom Codex model provider (found by
  `p0_p1_live` at `adb8b37`). Stage 3b did not close it: the deployment does
  not use multi-auth. `account/rateLimits/read` has no windows on this route,
  but the provider reports them in response headers, which the app-server
  forwards during the turn as `account/rateLimits/updated`. #102 uses those
  windows for the turn's response. Live-confirmed by `p0_p1_live` at
  `a0b27ff` and `f764ab8`. Closed.
- Out-of-band update and incident plane, owner decision 2026-09-30. **Planned,
  not implemented;** [ADR 0048](../decisions/0048-out-of-band-update-and-incident-plane.md) records it. Hub stays a passive observer: it
  keeps a bounded incident journal of alert episodes and reports version drift
  of the provider stack. Hermes reads that journal read-only and, without
  model inference, sends the owner a card per new episode: the quoted trigger,
  runbook options and their consequences from a per-alert catalogue. Model
  analysis starts only when the owner presses the card's button, which keeps
  maintenance rule 8. Stack updates go through a deterministic tool (stage,
  check, switch, rollback) that Hermes runs only on the owner's explicit
  command, as a separate unit, with a watchdog for Hermes' own updates. Stages:
  the tool and a private stack manifest; the drift check; Hermes integration;
  the watchdog.
- `configure-github.sh` required nonexistent check names (fixed in #82).
- Silent queue and execution waits: schema-37 repository implementation is
  under validation; [ADR 0051](../decisions/0051-accepted-turn-activity-and-queue-notices.md)
  records its scope and limits. Admission/handoff snapshots, accepted Codex
  activity, exact approval resolution and passive deadlines are implemented.
  Canonical validation and required independent review must bind the final
  candidate; source inspection and focused tests do not close live acceptance.
  Remaining work: active-work retry controls, approval before turn acceptance,
  broader provider/compatibility coverage, and separately authorized capacity
  and canary-root checks. No background inference or automatic replay is added.

- **Repeated notification/preparation failure and saved-payload retry:** source
  candidates and isolated native corpus are under review. The next trigger is
  exact-revision canonical/independent review of bounded transport consumption
  and schema-41 text-only notice-bound retry, then owner integration and a
  separately approved two-worker canary. Last verified published transport
  revision: `1a54af15d375992001579e328e8b31b5e1b9267d`; this is repository
  evidence only. Verify more than 1,024 foreign notifications and both results;
  two consecutive preparation failures must retain the original task/context
  through explicit retries, or visibly refuse when preparation replaced an
  existing thread without saving its effective context. Frozen fallback-context
  preservation and ordinary Reply historical context remain separate follow-ups.
  Restart alone does not close the item. Materials,
  safe unsubscribe, stdio buffering and exact deployed-source attribution remain
  open; no deployment or active-worker restart follows from this entry. See the
  [recovery procedure](QUEUE_RECOVERY.md#durable-dispositions) and
  [acceptance strategy](../testing/README.md).
  Independent Opus source review of retry head
  `fc1265286ab8b56b730cb371008ab9cc29d7a93b` completed. Its follow-up now
  protects failure publication when root resolution fails, derives Codex runtime
  from trusted configuration while retaining aliases, rechecks child selections
  and materials, and makes tickets update-immutable in unreleased migration 41.
  Astra reviewed the domain/transaction ownership and direct failure paths.
  Lease-crash and execution-crash regressions retain their distinct certainty;
  a real 65-source chain refuses at the ancestry bound without invocation.
  Final exact-head publication/review is pending. Explicit retries follow FIFO
  arrival order; they do not rewind the session. This source lane stacks on
  transport head `1a54af15`, separately from later transport/retirement/deadline
  siblings. Integration and live evidence remain open.

## Closure

Repeated Codex preparation overflow has source-level transport and saved-task
retry candidates, not deployment acceptance. The schema-41 text-only retry
candidate is published at `fc1265286ab8b56b730cb371008ab9cc29d7a93b`;
canonical publication validation passed, while required opposite-runtime review
and live gates remain open. It refuses thread-replacement retry ancestry that
lacks a frozen effective context. The stdio follow-up bounds both directions and
preserves accepted inbound frames at
`9a70840f362bd28f860c9bede44ecacefebd3307` (1,887 automated tests, canonical typing/privacy
and all seven hosted checks). Its actual Opus review found no blocker/high/medium,
but confirmed that immediate inbound sealing on writer failure could lose an
unread stdout tail. The response-channel follow-up below closes that source gap;
the older candidate alone is not full tail-conservation evidence.
The completed-socket retirement slice is in progress on that base: strict
single-use proof, publish-before-close, exact-client cache removal, optional
telemetry deadlines and cache-only queued catalogs passed focused checks and
architectural review. Eight offline native/observer cases passed with Codex
0.159.2, including surviving-peer operation, fresh metadata preparation under
over 1,024 foreign stream events and exact stored resume. Final publication and
required Claude review remain separate gates. The first Opus review requested
full idle-restoration/catalog context and explicit reconnect tradeoffs; follow-up
also guards queued clients against unrelated foreground poller cleanup.
Closeout requires a separately
authorized two-worker canary with more than 1,024 foreign notifications, both
results saved and two successive preparation failures retaining the original
task through explicit notice-bound retries. Restart alone does not close this
item. Absolute preparation/metadata deadlines, client-event
byte bounds, material retry and exact deployed-source attribution remain open.
See [queue recovery](QUEUE_RECOVERY.md) and the
[acceptance strategy](../testing/README.md).

### Completed-socket slice ownership and extraction

Owner: lead agent / Hub maintainer. Source lane
`fix/codex-successful-unsubscribe` owns this bounded slice, despite its historical
branch name; it performs connection retirement without an unsubscribe RPC.
Base/last canonical verified revision is
`9a70840f362bd28f860c9bede44ecacefebd3307` on `fix/codex-stdio-backpressure`.
The native notification test dependency comes from the independently reviewed
source corpus at `dc289f816cb6f83ae36641f89af857aa75cefd1e`; this slice shortens
its disposable endpoint directory to fit the Unix socket pathname limit
when the real Hub transport resolves a pinned descriptor, and stamps the scripted
message's final-answer phase for exact stored-output assertions.
After integration, recheck tracked/staged/untracked lane state before any removal.
Next trigger: exact clean publication, required Claude review, then an explicitly
authorized integration and live canary. No deployment acceptance is claimed.

Architecture review retained HubState as transaction owner, workers/Controller
as invocation and cache owners, and transports as connection cleanup owners.
`codex_connection_completion` owns exact completion proof;
`codex_result_lifecycle` owns optional context/quota and survived cleanup/report
failures; `inline_codex_execution` owns legacy inline invocation and its completion
journal. This extracts one shared lifecycle instead of copying policy across
three runtime paths and reduces Controller responsibility. External execution
keeps the explicit result checkpoint/publication/retirement order; its reviewed
207-line exception is recorded in `hotspots.json`. Next extraction review remains
stabilization stage 3 or any new lifecycle/transaction/invocation branch.
Failure before publication retains existing certainty/recovery rules; cleanup
after publication cannot cause productive replay. Stdio retirement awaits a
durable owned-process exclusion barrier and is outside this slice.

### RPC deadline follow-up ownership

Owner: lead agent / Hub maintainer. Source lane `fix/codex-rpc-deadlines`
owns fixed per-RPC response deadlines, with base/last verified source revision
`3b40ae80d8bc34f2ae4105d10b6c75085d6e43c6` (1,915 commit-gate tests; canonical
publication and opposite-runtime review are separate gates). State/transaction,
invocation, completion-proof and cleanup ownership remain unchanged. The
bounded policy is in the protocol client; no new retry authority, schema,
shared preparation budget or provider inference is added. Architecture review
approved default 120-second response/20-second quiet bounds and explicit
300-second turn submission with existing caller deadlines preserved.

Next trigger: exact clean publication and independent review, then integrated
regression and the separately authorized canary. After integration, inspect
tracked/staged/untracked lane state before changing or removing the worktree.
See [RPC recovery procedures](QUEUE_RECOVERY.md#codex-rpc-response-deadlines) and
the [testing guide](../testing/README.md). Native/live acceptance remains open;
legacy inline uncertainty, event-byte budgets and a durable owned-process
barrier including idle stdio restoration remain separate work.

### Stdio response-channel follow-up ownership

Owner: lead agent / Hub maintainer. Lane `fix/codex-stdio-tail-conservation` is
based on clean `ebd92469f0701c70539ce95a70bcbbd445868d62`, the independently
reviewed RPC-deadline candidate (1,926 canonical tests, typing/privacy and seven
hosted checks). This lane does not include the sibling schema-41 saved-task retry
candidate; integration must verify both together. Work is in progress: focused
synthetic regressions pass, while exact clean publication and opposite-runtime
review remain separate gates. No deployment or live acceptance is claimed.

Transport owns first-cause selection, bounded FIFO and pipe cleanup. The client
owns native submission/accepted identity, visible callbacks and completion;
dependency-neutral `codex_response_drain` owns the narrow deadline, notice and
fault-time proof policy. State and worker transaction/invocation ownership are
unchanged. Independent Astra review closed quiet asynchronous fault detection,
accepted-turn matching and partial-text retention at deadline expiry; it found
no remaining blocker/high/medium in that diff. The extraction avoids copying
fault policy across worker paths and requires no new hotspot exception.

Next trigger: exact clean publication/review, integrated regressions, then the
separately authorized two-worker canary above. Include approval before final
under a broken stdio response channel and verify the saved result carries the
fixed Hub notice without a grant or another invocation. Inspect tracked, staged
and untracked lane state after integration before any worktree change/removal.
See [queue recovery](QUEUE_RECOVERY.md) for the 20-second observed-fault drain
and its late-tail limits. Traceback retention and descendant-held pipe cleanup
remain open; restart is still temporary recovery, not incident closure.

The plan closes when stages 0–3 are merged into `main`, the hotspot ratchet
runs in the canonical gate, and the owner has either completed or explicitly
re-scoped every backlog item. Record the closing revision here.

Protected Claude file tools: live activation also requires independently proven
OS separation of untrusted principals from receipt keys/state/endpoints; the
current symmetric transport does not protect against an unconfined same-UID
actor. See [ADR 0052](../decisions/0052-protected-claude-file-permissions.md).
