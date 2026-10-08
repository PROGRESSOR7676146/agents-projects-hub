# ADR 0060: Final and progress Telegram delivery certainty

Status: accepted design; source implementation under validation.
Date: 2026-10-08.
Owner: Hub maintainer; product decision owner: repository owner.

## Context

The final/progress paths fabricated ID 1 when the client returned no receipt,
and broad retry catches combined transport, receipt commit and cleanup. A network
timeout could therefore resend an accepted message. Stale leases had no durable
distinction between preparation and an attempted send. Later Codex terminal
observation could replace an unknown notice and erase its multipart evidence.

The owning contract is [REQ-QUEUE-005](../product/PERSISTENCE_AND_RECOVERY.md).
Control notices already have a send-start fence; this change applies the same
certainty policy to final/progress paths without another queue or sender.

## Decision and ownership

Schema 43 adds unknown delivery and nullable send-start timestamps. The existing
DeliveryStateFacade on the HubState connection owns begin/receipt/unknown CAS.
HubState owns each immediate transaction and exposes the existing facade through
one read-only property. A final begin verifies the exact expected unresolved part
and unexpired lease. Strict positive integer receipts commit the part and version1
provenance together. Mark-unknown accepts an expired matching attempted lease,
but cannot overwrite changed ownership, a committed part or the next multipart
pending state. Unknown does not change provider-job status or execution evidence.

The shared final_delivery helper owns local validation, fence, transport, receipt
commit and subsequent artifact cleanup. External and embedded paths retain their
short lease/stop/health wrappers. This removes artifact handling and duplicated
failure policy from Controller. Progress has its own short helper. Transport-only
rejection classification lives in delivery_retry: explicit API4xx except408,
or HTTP429. Receipt-commit errors never use that classifier. HTTP5xx and malformed
success remain unknown. Persisted retry deadlines still honor retry_after.

Migration43 rebuilds final parent and child together with FK enabled, copies
explicit columns, drops the old child before parent, renames replacements, and
restores indexes/triggers. Legacy sending is unknown with no invented start time;
legacy parts receive validation version0. Progress rebuild has no incoming FK.
The migration runner retains its backup/BEGIN IMMEDIATE/rollback ownership and
checks both integrity and foreign keys before commit. Historical migrations are
unchanged. Runtime rollback requires a schema43-compatible artifact.

Late native observation takes an evidence-only branch for unknown/attempted
notice delivery. It validates the existing exact binding, retains old delivery
rows/spool, saves independent terminal proof and exact completed checkpoint text,
holds queued tails, finishes applicable stop certainty and removes the observation
in one transaction. It keeps the job indeterminate and creates no result row or
replacement send. Existing observer/recovery selection cannot automatically
reclaim it. No new artifact snapshots are claimed as retained without references.
Outcome diagnostics expose notice delivery separately from result delivery and
show per-part provenance completeness. Passive unknown counters/alerts contain
no content or project identity. They do not authorize an action.

## Verification and limits

Focused tests cover malformed/bool receipts, external/embedded/document/progress,
pre/post-fence expiry, commit faults, committed multipart prefix, cleanup faults,
deadline clocks, exact terminal observation, and populated schema42 migration
with triggers, deferred-FK failure and DDL rollback after old-child deletion.
Canonical validation, exact-candidate independent reviews and hosted checks are
required before publication completion. Telegram/native deployment acceptance is
separately authorized. This prerequisite supplies delivery evidence for future
owner assessment; it implements no assessment command, judge, scoring or advisor
workflow. Unknown delivery reconciliation remains explicit, never blind resend.
