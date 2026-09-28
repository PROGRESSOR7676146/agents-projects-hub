# Stabilization plan

Status: active; stages 0–1 in review, stage 2 in progress
Date: 2026-09-27
Owner: repository owner (decisions and merges); Claude Code executes
Last verified revision: `9948eac` (stage 1 head, canonical gate passed)
Next trigger: owner review and merge of the stage 0–1 pull requests

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
| 0 | Fix the lifecycle test broken since `2d24e82`; review the Codex branch | In review: #80 | Canonical gate at `6ce927e`; review findings 1, 3, 4 fixed |
| 1.1 | Parallel isolated test modules; working focused selectors; pre-commit gate | In review: #81 | ADR 0041, ADR 0042; suite ~45 s instead of ~340 s |
| 1.2 | Bounded diagnostics for survived failures; Ruff S110/S112 | In review: #83 | ADR 0043 |
| 1.3 | CI from `uv.lock`; non-required coverage report | In review: #82 | Hosted run on 3.11–3.13 and coverage job green |
| 1.4 | README version checked by the release metadata audit | In review: #82 | Audit test |
| 2a | `CLAUDE.md`; review/merge rules; risk register; ADR 0016 tombstone; this plan | In review: #84 | — |
| 2b | Hotspot growth ratchet in every validation profile; rules 12 and 14 amended | In review: #85 | ADR 0044; 21 recorded hotspots |
| 2c | Capability summary table in project status; closure sections for six completed plans | In review | Prose still to move into owning modules |
| 3 | Extract `_handle_update`, `load_hub_config`, `cli.main`; remove dead code; tests for weak modules; injected clocks in timing tests | Characterization in review: #89 | Dispatcher coverage 316 → 359 of 421 |
| 3b | Retire the multi-auth integration (reliability package B): migration contract, retired keys rejected before helper access, superseded ADRs | In review | ADR 0047; no schema change |
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
| Durable root blockers (schema 35) | REQ-WRITER-008 | Blocked input, held job, owner decision |
| Exact Codex turn recovery (schema 34) | REQ-QUEUE-004 | Uncertain turn, read-only proof, continuation |
| Root concurrency, worktree lanes, Codex slots | REQ-QUEUE-002, REQ-QUEUE-003 | Three projects on independent roots |
| Project-group provisioning | REQ-ONBOARD-003, REQ-ONBOARD-006 | New project canary |
| Registered-project editing | REQ-PROJECT-EDIT-001..004 | Rename and relocation canary |
| Machine-loss recovery drill | REQ-OPS-012 | Private cold-restore drill |
| Unauthorized access and disclosure | REQ-SEC-006, AC-F-007, AC-NF-001 | Outsider-account negative checklist |

## Follow-ups found during execution

- Reliability plan packages B–E: owner decision 2026-09-27 — B, retiring
  multi-auth, moves into this plan as stage 3b; C, D and E are deferred and
  stay recorded in the reliability plan.
- Claude failure classification (PR #80 review, item 2), owner decision
  2026-09-27. **Planned, not implemented;** it is to be implemented with the
  Claude parity work. Decided target: an unknown outcome keeps the root hold
  and never replays; a confirmed, structured terminal quota or provider
  rejection gets its own classification, also without replay. Current
  scaffold behavior: any unsuccessful terminal result becomes a generic
  runtime error and the job is recorded as `indeterminate`
  (`ambiguous_execution`), holding the root until the owner resolves it.
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
  `p0_p1_live` at `adb8b37`): owner decision pending. Stage 3b does not close
  it: the deployment does not use multi-auth, and the windows are missing
  because the app-server on this route reports none. Options: read the windows
  from another passive local source on this route, if one exists; or the P0/P1
  check accepts a missing label on a route without provider rate-limit
  telemetry.
- `configure-github.sh` required nonexistent check names (fixed in #82).

## Closure

The plan closes when stages 0–3 are merged into `main`, the hotspot ratchet
runs in the canonical gate, and the owner has either completed or explicitly
re-scoped every backlog item. Record the closing revision here.
