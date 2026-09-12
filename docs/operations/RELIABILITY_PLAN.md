# Reliability implementation plan

Status: repository milestones complete; observation and host-reboot evidence continue gradually

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
4. **Release gates — repository-complete.** Sanitize fixtures, reconcile
   acceptance wording and the requirements manifest, and run canonical
   validation including reachable-history privacy checks. No allowlist or
   disabled privacy check is acceptable.
5. **Operational quality — repository-complete.** Add passive counters for delivered finals, partial
   outcomes, uncertain execution, recovered results, delivery delay and queue
   age. Durable rate-limited visible progress now has a separate queue and
   passive age threshold. Delivery never changes the provider-job outcome.
   Lifecycle and alert policy have begun moving from the largest modules into
   focused contract-tested modules. Retire compatibility paths only after
   acceptance and rollback requirements.
6. **Deployment acceptance and recovery — reusable gates implemented.** For
   each deployment, prepare an exact clean candidate and compatible rollback
   artifact, then perform separately authorized deployment, bounded
   Telegram/provider E2E and off-machine restore drills. Keep all real deployment
   evidence outside Git. No background model probes. A read-only indeterminate
   audit and fail-fast Hub operations identity validation are included before
   the bounded live acceptance run.

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

## Acceptance sequence

Each release candidate must pass the canonical repository gate, use an exact
clean wheel and a compatible rollback wheel, complete the offline rollout and
rollback rehearsal, and then prove the required deployed components report the
same revision. Private deployment evidence remains outside Git. A bounded live
Telegram check supplies end-to-end evidence; it does not replace the offline
fault and privacy gates. The remaining 48–72 hour observation and a real host
reboot are recorded gradually and do not weaken the revision-specific evidence.
