# Stabilization plan

Status: active; stages 0–2 and 3b done, stage 3 in progress
Date: 2026-09-27; updated 2026-10-03
Owner: repository owner (decisions and merges); lead development agent integrates
Last verified integrated repository revision:
`3750ccfb0f8eb98333f9219f3a697d6328890d04` for stop, native Claude and schema-37
visibility (canonical and hosted checks, after independent candidate reviews).
The schema-38 protected file-tool candidate is implemented offline; follow
[ADR 0052](../decisions/0052-protected-claude-file-permissions.md) and the
[candidate runbook](CLAUDE_FILE_PERMISSIONS.md). Publication/review gates and
separately authorized native/Telegram acceptance remain open.
Next trigger: the native human approval boundary in the
[continuation plan](NEXT_DEVELOPMENT_SESSION.md); continue the separately
authorized live backlog and ADR 0048 work.
Deployment identity and private acceptance records remain outside this plan.

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
| Saved-session `/connect` | REQ-CMD-008, REQ-WRITER-012, AC-F-013 | Topic, Hub-private and local-code entry paths |
| Accepted-turn activity and queue snapshots (schema 37) | REQ-QUEUE-012, REQ-QUEUE-013 | Queue blocker, long tool, approval resolution, restart and ambiguous delivery at the exact deployed revision |
| Durable root blockers (schema 35) | REQ-WRITER-008 | Blocked input, held job, owner decision |
| Exact Codex turn recovery (schema 34) | REQ-QUEUE-004 | Uncertain turn, read-only proof, continuation |
| Root concurrency, worktree lanes, Codex slots | REQ-QUEUE-002, REQ-QUEUE-003 | Three projects on independent roots |
| Project-group provisioning | REQ-ONBOARD-003, REQ-ONBOARD-006 | New project canary |
| Registered-project editing | REQ-PROJECT-EDIT-001..004 | Rename and relocation canary |
| Machine-loss recovery drill | REQ-OPS-012 | Private cold-restore drill |
| Unauthorized access and disclosure | REQ-SEC-006, AC-F-007, AC-NF-001 | Outsider-account negative checklist |

## Follow-ups found during execution

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
- **Project-topic incident collaboration (new owner request; design/implementation pending).**
  Extend the passive incident plane to classify durable queue, provider, delivery,
  approval and Telegram topic problems, without scraping private topic messages
  into a second monitoring store or starting model turns in a timer. Preserve
  exact numeric topic/job provenance in private operator context and route one
  bounded, redacted handoff to the responsible development thread only after
  checking its binding, queue and existing ownership. The handoff must distinguish
  Hub's state from exact provider terminality and avoid a second productive turn
  behind unresolved work. Owner decision: keep monitoring passive; Hermes
  investigates on an explicit message or button, not from unattended alerts.
  Background LLM triage remains forbidden by maintenance rule 8 and ADR 0048.
  Acceptance: simulated duplicate/error storms produce one deterministic card;
  wrong-topic and active/uncertain-turn cases do not send or restart; a real
  authorized handoff is read back from Telegram; a controlled restart proves
  drain, backup, schema-compatible rollback, exact revision convergence and
  post-restart delivery without replay. Existing Operations alerts remain the
  independent fallback if Hermes is unavailable. No general standing restart or
  deployment authority is implied by topic coordination.
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
- **Recovery after bounded observation (planned; lead development agent).** An
  accepted turn may remain `active` when the bounded read-only observation budget
  expires and later become terminal. Preserve its root exclusion while active,
  but provide an owner-visible, model-free exact-turn recheck and an idempotent
  path to publish the stored completed result or corrected terminal-failure
  notice. Test late completion after the final automatic observation, worker and
  Controller restart, concurrent sender lease, and a second topic on the same
  root. Never turn an old uncertain job back into queued productive work. The
  [recovery runbook](QUEUE_RECOVERY.md#provider-job-recovery) must explain the
  distinction between the Hub job's uncertain label and the provider turn's
  current status; a one-time observation is not a promise of ongoing monitoring.
- **Owner-authorized cross-topic continuation (planned; lead development agent).**
  A separate Telegram user account may be allowlisted as an owner even when its
  MTProto configuration was created for a single acceptance topic. Do not treat
  that test configuration as a general sender or the secondary account as the
  primary owner identity. Specify a reviewed, explicit operator action for one
  exact registered project/topic and delivered provider Reply target, with
  account-identity and authorization checks, duplicate-send protection,
  Telegram readback, and exactly one Hub admission. Refuse absent scope,
  unconfirmed provider terminality, wrong topic, missing Reply provenance, or
  ambiguous send. Keep credentials and live identifiers outside Git. Extend
  the [testing guide](../testing/README.md#dedicated-acceptance-user) and
  [recovery runbook](QUEUE_RECOVERY.md#provider-job-recovery) to distinguish
  the fixed canary actor from this exceptional owner-requested continuation;
  cover a real Reply without bot-to-bot messages or direct provider invocation.
- **Approval/release recovery rehearsal (planned; operations owner).** Cover
  the shared Codex socket appearing *after* isolated workers start, recovery
  from latched stdio fallback between turns, and a cold-boot `PrivateTmp` bind
  source. Require a schema-compatible distinct rollback artifact before a
  schema-advancing cutover; the old installed release is not automatically a
  valid rollback. Verify queue/uncertain-work drain and the exact live transport,
  then separately observe human Deny and Allow through the companion. Update the
  [live canary](LIVE_CANARY.md) and release/recovery instructions to separate
  offline wheel rehearsal from deployed approval acceptance. Do not interrupt
  active turns, weaken the sandbox, or infer human approval from socket presence.

## Closure

The plan closes when stages 0–3 are merged into `main`, the hotspot ratchet
runs in the canonical gate, and the owner has either completed or explicitly
re-scoped every backlog item. Record the closing revision here.

Protected Claude file tools: live activation also requires independently proven
OS separation of untrusted principals from receipt keys/state/endpoints; the
current symmetric transport does not protect against an unconfined same-UID
actor. See [ADR 0052](../decisions/0052-protected-claude-file-permissions.md).
