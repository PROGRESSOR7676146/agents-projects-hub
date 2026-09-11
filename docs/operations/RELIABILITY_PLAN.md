# Reliability implementation plan

Status: repository milestones one through five implemented locally, 2026-09-11

Objective: each admitted task has a visible, accurately classified outcome;
already-produced results survive delivery failures without replaying side effects.
Source findings are in [the quality review](QUALITY_AND_STABILITY_REVIEW.md).

## Milestones and acceptance

1. **Immediate result handling — repository-complete.** Add failing
   regressions, consume buffered Codex completion events, deduplicate visible
   items, retain bounded partial text on handled turn failure, publish a safe
   cause with the incomplete result through the existing durable outbox, and
   distinguish caught preparation failures from uncertain invocation. Share
   Telegram retry policy across both senders and respect `retry_after` across
   restart. Test no second provider invocation and no false successful result.
   This batch does not claim partial-text durability across abrupt worker death.
2. **Durable execution recovery — repository-complete.** Add a versioned migration for execution
   identity and bounded visible-item checkpoints, separate from immutable job
   admission snapshots. Persist thread creation and turn acceptance separately.
   Implement exact-turn read-only reconciliation using verified provider
   capabilities, never synthetic inference. Test crashes before/after each
   boundary, completed-result recovery, unknown acceptance, and schema-compatible
   artifact rollback. Retain uncertain side effects and explicit continuation.
3. **Transport lifetime — repository-complete.** Enforce timeouts on real stdio reads, propagate
   graceful and abrupt WebSocket closure, and test a silent fictional child.
   Distinguish useful progress, approval waiting, and process heartbeat; do not
   kill productive work solely because no final text has arrived.
4. **Release gates — current tree repaired; history blocked.** Sanitize current
   fixtures, format changed files, reconcile
   acceptance wording and the requirements manifest, and run canonical
   validation. Reachable private Git history requires a separately authorized
   history correction; no allowlist or disabled privacy check is acceptable.
5. **Operational quality — repository-complete for outcome telemetry.** Add passive counters for delivered finals, partial
   outcomes, uncertain execution, recovered results, delivery delay and queue
   age. Extracting more duplicated lifecycle policy remains incremental
   maintenance under contract tests. Retire compatibility paths only after
   acceptance and rollback requirements.
6. **Deployment acceptance and recovery.** Prepare an exact clean candidate and
   compatible rollback artifact, then perform separately authorized deployment,
   bounded Telegram/provider E2E and off-machine restore drills. Keep all real
   deployment evidence outside Git. No background model probes.

## Scope and execution discipline

Development, offline tests and reversible documentation changes are authorized.
Do not conflate them with history rewrite, publication, service activation or
live acceptance. Finish a tested repository checkpoint before any deployment
decision. Keep schema migration and release-artifact compatibility in one reviewed
batch, rather than silently extending schema 21 in place.

Delegate small bounded test-matrix or source lookups to agy when useful. Empty,
denied or timed-out output supplies no evidence. Final integration reads code
and verifies actual behavior directly. Existing tools and project instructions
suffice; no new skill is required.

## Verified development checkpoint

Baseline HEAD remains `634b0e9c2400fd68feb709f0745454654b4ec0ab` on
`release/0.7.0-rc`; no commit, history rewrite, push or deployment was performed.
`AGENTS.md` was already dirty and remains untouched by this work. The prior
quality-review documents and this batch's code/tests/product documentation are
uncommitted; do not label this a clean release revision. Deployed components
were not restarted or revalidated during implementation.

Validation in the repository Python 3.11 venv:

- Twenty-five regressions were added across result handling, crash boundaries,
  handled-disconnect recovery, schema rollback, transport deadlines, passive
  outcome telemetry and recovery-capsule compatibility.
- Full unittest discovery: 531 tests, OK, three existing Unix-socket tests
  skipped because the sandbox blocks socket creation.
- Ruff lint, Ruff format, Pyright, documentation contract and release metadata
  passed. Metadata still reports missing release-tag debt.
- Current-tree privacy scan: zero findings. Canonical
  `.venv/bin/python scripts/validate.py` still fails because the old fixture blob
  is reachable from history. The scanner is unchanged and was not bypassed.
- The requirements manifest was updated only for reviewed sections 13 and 15
  and the existing section 19 no-live-probes rule, preserving that rule.

The single bounded agy test-matrix job timed out with no result; no claim relies
on it. Direct tests supplied the evidence. Reusing existing fixtures initially
also imported their test classes into discovery; imports were corrected to
modules so the final count does not double-count those suites. Existing test
helpers suffice; no new skill or service was installed.

Milestones two and three now add schema-22 crash recovery, exact-turn recovery
after handled disconnects, bounded stdio reads and deterministic WebSocket
closure. The passive monitor exposes aggregate reliability counters and queue
ages. Before release, obtain a separate decision on correcting the affected
historical commit, build a compatible schema-22 rollback artifact, then rerun the
complete canonical gate. Deployment and live acceptance remain a separate task.
No temporary provider or review service is left running.
