# ADR 0063: Exact delivery control reconciliation, staged prerequisite

Status: accepted staged design; schema46 prerequisite published, schema47 authority under validation.
Date: 2026-10-08.
Owner: Hub maintainer; product decision owner: repository owner.

## Context

Schema44 permits queue continuation past one unknown final delivery while
retaining indefinite control and topic-binding restrictions. Operational recovery
needs a separately explicit decision, rather than silently expanding that consent.
[REQ-QUEUE-005](../product/PERSISTENCE_AND_RECOVERY.md) owns this boundary.
The prior [decision](0061-owner-delivery-hold-dispositions.md) remains unchanged.

## Staged decision

Schema46 first publishes only an additive immutable ledger and coherent read-only preview.
That prerequisite has no public or hidden record/apply helper, consumer exemption, resend,
replay, fabricated receipt, new Reply authority or assessment authority. Output
in schema46 always declares `preview_only`, `apply_available=false`, `control_effect=not_enabled`.
Seeded test rows confer no runtime authority in that prerequisite revision.

Schema46 adds `telegram_delivery_control_dispositions`. A separate decision ID,
NULL-safe target branches and unique nullable foreign keys retain either an exact
final outbox or progress delivery and its commentary item. One job can have many
distinct targets. Final retains its nullable result identity; progress has no result
link. The decision retains project, topic/sequence, destination, sender and historical
session/generation, action/version/hash, local-owner authority, timestamp and scope
at consent. It does not claim that scope is the original execution root.
UPDATE/DELETE triggers preserve the decision; target/proof FKs have no cascade.
No project-registry table or Telegram actor is invented.

`DeliveryControlState` reads on the caller-owned connection and requires an open
transaction. HubState owns BEGIN, commit and rollback; nested preview refuses
without ending the caller transaction. CLI `delivery-control STATE KIND TARGET`
opens existing current-schema state read-only and never loads configuration,
credentials, provider or Telegram clients. Digest content and paths remain private.
Small reviewed facade/registration exceptions keep this boundary; LaneState already
owns lane SQL/policy and no general transaction framework is introduced.

Only parked unknown or exhausted failed targets without any sender lease qualify.
The existing final/progress attempt limit is 20, separate from provider-job attempts.
A send fence may remain on unknown; legacy missing fences do not prove delivery.
Final job/result and progress exact commentary-item consistency are verified against
the historical session, job and numeric destination, without a current active-session
requirement. Scope must be an established canonical string; preview performs no
filesystem resolution or registry mutation.

Snapshot version1 covers the complete target/job, verified binding and current
scope, final result and all ordered parts without a page limit, receipt/artifact
metadata, or progress delivery/item, plus retained checkpoint, origin, terminal
evidence and owner resolution. Missing rows are explicit NULL. Deterministic JSON
hashing adds no preview timestamp. Fresh and recorded tokens stay separate.
Private historical matching compares exact target/job/project/topic/destination/
sender/session generation and final nullable result or progress item. It excludes
live scope, current active session, session model/status and a later progress result.
In schema46 it is a diagnostic boolean, never a runtime SQL predicate.

## Failed-notice retention prerequisite

Late native reconciliation can legally archive/delete a failed indeterminate
notice and create a replacement. First apply may pin that target only
after independent exact terminal evidence or an existing owner resolution already
excludes replacement. The preview checks this now. Evidence must match the retained
checkpoint's job, thread, turn and canonical saved root, with terminal status
completed/failed/interrupted. A stopped completion needs neither a saved result nor
completed text. Resolution validates exact job and known classification and does
not require a checkpoint. Current live scope is not historical native root evidence.
Nullable proof links retain the selected prerequisite in the ledger. FKs prevent
deletion or key replacement, not arbitrary proof-field updates; existing resolution
API is insert-once, without SQL immutability triggers. Full proof rows enter the
snapshot. Unknown notice evidence-only recovery remains unchanged. Earlier consent
for a replaceable failed notice would need a separate tombstone lifecycle slice.

## Coordinated schema47 full-control boundary

Schema47 implements public apply, exact snapshot CAS and coordinated consumer changes.
One immediate HubState transaction must revalidate and insert only the disposition.
Apply inserts only the ledger row, never a delivery/job/receipt mutation.
An exact repeated token returns its original immutable decision and current effect;
another token cannot replace it, and another target requires another consent.
Legitimate later session/scope changes must preserve historical target effect.
Every schema44 lifetime scope freeze requires individual exact full-control coverage;
old consent is never rewritten. Frozen additive schema47 DDL owns the trigger
change; published schema46 DDL remains unchanged. The new guard still refuses a
prospective project or numeric destination change, even if historical full coverage
exists. Runtime and frozen trigger both revalidate parked target, exact historical
binding, admissible final job/result and retained failed-indeterminate prerequisite.
Later legal session/model/agent/scope changes preserve the historical effect;
unsupported job/result, project, destination or generation drift fails closed.

This slice covers session/model/agent controls, writer/local transfer,
connect/adoption, lane/source/destination scope, relocation, drain and compatibility
consumers together. Raw unknown/failed totals stay visible, independent native
uncertainty, owner holds, stop, writers, capacity and workflows retain their own
guards. Adoption and relocation retain their existing owner-resolution-only
indeterminate policy. Progress consent never releases a result-ready final wait.
Task/control-notice delivery is not an eligible target. Partial activation is
prohibited. Legacy queue-only preview/retry projects any separate full effect
without relabelling its immutable permission. Status delivery counts share one
SQL snapshot; raw aggregates and effective drain/alert counts remain separate.
True final exhaustion uses the existing retry API and leaves the job failed with
telegram_delivery classification, not a fictional result_ready state.

## Verification and activation

Tests cover populated45-to46 and46-to47 migration/private backup/DDL rollback, immutable
NULL-safe branch/FK invariants, both parked types, exhaustion/leases/binding,
indeterminate prerequisites and retention, all-part/provenance/artifact fingerprints,
coherent concurrent reads, historical matching, exact CAS, per-target coverage,
consumer predicates and independent barriers. Schema46 prerequisite evidence is
published at `357409b0d52a6796a3fe609f9f49c2b3bb918817`; its lack of authority
does not substitute for the coordinated schema47 tests and exact review.
Exact-source Astra and actual independent Claude review, mandatory canonical and
hosted checks are publication gates. Custody remains a separate OS boundary:
SQL triggers and local CLI access are not same-UID authentication or isolation.

Schema46 binaries cannot open schema47. Deployment must first prove compatible
rollback artifacts; a pre-migration backup never authorizes destroying later
accepted writes. Source publication, merge or schema number authorizes no live
migration or activation. Full authority requires completed consumers/public CAS,
independent exact-source review and separately owner-authorized deployment and
acceptance on an exact revision.
