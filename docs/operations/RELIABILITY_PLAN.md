# Reliability implementation plan

Status: repository reliability milestones, package A, and the package-F exclusion prerequisite complete locally; hosted Actions evidence remains pending

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
4. **Repository validation gates — repository-complete.** Sanitize fixtures, reconcile
   acceptance wording and the requirements manifest, and run canonical
   validation including reachable-history privacy checks. No allowlist or
   disabled privacy check is acceptable. This milestone did not gate GitHub
   release publication on the Python CI matrix; that separate gap is first below.
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
batch, rather than silently changing the existing schema version in place.
The current schema is 26; later stateful features must not hide additive schema
changes under that version.

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

## Approved next development batch

The six approved directions remain in scope, but they are not one implementation
session or one release. Package A and the root-exclusion prerequisite of package
F are implemented and locally reviewed in the
repository, as recorded in the [detailed execution plan and review](NEXT_DEVELOPMENT_SESSION.md).
Later packages have explicit readiness criteria; approval of a direction does
not supply its missing state machine. This planning revision changes no
implemented capability or normative requirement lifecycle label beyond package
A's documented repository evidence.

Development does not authorize deployment, service or credential changes,
publication, tag changes, history rewrites, or live provider calls. Preserve the
release baseline and inspect current Git state before implementation; old handoff
claims do not establish current remote or deployment health.

| Order | Package and deliverable | Completion boundary |
| --- | --- | --- |
| A — locally complete | One reusable Python 3.11–3.13 validation matrix, release publication dependent on its success, and demonstrated SQLite resource cleanup. | Canonical validator includes the release-lock check in CI; publication checks the same commit; focused failure tests and full local gates pass on Python 3.11, 3.12, and 3.13. Review covers failed-backup cleanup, metadata-reader/fixture connection ownership, and validation-bypass regressions. Hosted Actions execution remains unproven. No schema change. |
| B | Retire multi-auth runtime/configuration integration with a supported-transport and configuration migration contract. | Preserve official stdio and independent shared-socket approval behavior; retired keys fail before filesystem/helper access; no stale pool catalogs or alerts. Update normative requirements and supersede affected ADRs explicitly. |
| C | Extend existing project validation and acceptance machinery for onboarding. | A bounded result separates offline preflight/synthetic routing from live routing, real restart and response-identity evidence; unavailable live checks cannot make a project accepted. No second acceptance framework. |
| D | One measured, cohesive extraction justified by work in B or C. | Preserve transaction boundaries, the current schema contract, public APIs and observable behavior; demonstrate a specific reduction in coupling or duplication. This is not a prerequisite for all other work and may be omitted if no useful seam is found. |
| E | Durable controls for one concrete pending-decision scenario. | Define decision creation, action meanings, free-text correction, expiry, crash boundaries and atomic callback-to-job transition first; then prove owner/topic/session scoping, deduplication and first-valid-answer-wins. No timer or approval substitution. |
| F — exclusion prerequisite locally complete; concurrency pending | Bounded concurrency across independent project roots/explicit worktree lanes, default one. | Schema 26 now provides transactional canonical-root exclusion across Hub queue providers, local ownership and adoption, with contention/crash/migration tests. Before increasing worker count, separately design and prove per-slot resource ownership, health identity, fairness, shutdown and recovery. |

Before E, reconcile the stale fixed-delay wording in REQ-UX-007 with accepted
[ADR 0010](../decisions/0010-no-mandatory-grace-period.md). Start/Clarify/Cancel
labels alone do not define a pending-decision protocol. Do not add a generic
model-command interpreter or an unconditional pause before ordinary work.

For B, retirement is a product compatibility change, not a host uninstall.
Document which legacy keys are rejected, how configuration is migrated, and
which generic transport/telemetry capabilities remain. Preserve historical ADR
rationale with explicit supersession; do not erase every textual mention.

For F, a different Telegram topic does not prove a different filesystem lane.
The current `lease_provider_job` query enforces topic ordering, not exclusion of
different topics sharing a root. Do not turn up worker count until the exclusion
contract is enforced for all productive providers and local writer ownership.

The 48–72 hour observation, real host reboot, and off-machine restore remain
separate deployment work and evidence. Do not call the product operationally
mature on the strength of these repository packages alone.
