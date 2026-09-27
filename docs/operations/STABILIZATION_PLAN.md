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
| 3b | Retire the multi-auth integration (reliability package B): migration contract, retired keys rejected before helper access, superseded ADRs | Planned | — |
| 4 | Release 0.8.0; branch and worktree hygiene | Planned; each action needs owner approval | — |

## Live-acceptance backlog

Each item needs deployment-local evidence at an exact clean revision, recorded
privately (see [live canary](LIVE_CANARY.md)). Repository tests are not
acceptance.

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
| Codex quota transition | REQ-AUTH-007 | Natural or controlled exhaustion |
| Machine-loss recovery drill | REQ-OPS-012 | Private cold-restore drill |

## Follow-ups found during execution

- Reliability plan packages B–E: owner decision 2026-09-27 — B, retiring
  multi-auth, moves into this plan as stage 3b; C, D and E are deferred and
  stay recorded in the reliability plan.
- Claude failure classification (PR #80 review, item 2), owner decision
  2026-09-27: an unknown outcome keeps the root hold and never replays; a
  confirmed, structured terminal quota or provider rejection is classified
  separately, also without replay. Implemented with the Claude parity work.
- `codex-worker@1` / `claude-worker@1` duplicate slot 1 of `worker@codex` /
  `worker@claude` (PR #80 review, item 5).
- Slot identity format and bounds are duplicated in four modules (PR #80 review, items 7–8).
- Real-clock lease tests fail when the host suspends (R-018).
- Product principle 10 and the capability matrix contradicted the Claude
  scaffold; resolved by withdrawing the gate (ADR 0045).
- An analysis of the Claude integration path to Codex parity follows stage 3.
- `configure-github.sh` required nonexistent check names (fixed in #82).

## Closure

The plan closes when stages 0–3 are merged into `main`, the hotspot ratchet
runs in the canonical gate, and the owner has either completed or explicitly
re-scoped every backlog item. Record the closing revision here.
