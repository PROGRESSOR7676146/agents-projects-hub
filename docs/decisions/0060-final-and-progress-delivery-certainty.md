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

The owning contract is [REQ-QUEUE-005](../product/DURABLE_QUEUE_AND_CONTROL.md).
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

Late native observation defers every sending notice, including a begun send,
until receipt, proven rejection or stale recovery settles it. It does not consume
the observation or write terminal evidence while the send remains in flight.
Only parked unknown takes an evidence-only branch for retained
notice delivery. It validates the existing exact binding, retains old delivery
rows/spool, saves independent terminal proof and exact completed checkpoint text,
holds queued tails, finishes applicable stop certainty and removes the observation
in one transaction. It keeps the job indeterminate and creates no result row or
replacement send. Existing observer/recovery selection cannot automatically
reclaim it. No new artifact snapshots are claimed as retained without references.
Outcome diagnostics expose notice delivery separately from result delivery and
show per-part provenance completeness. Passive unknown counters/alerts contain
no content or project identity. They do not authorize an action.

Normal replacement of a certain notice archives every part under the existing
recovery-notice parent in the same transaction. The archive retains exact part
identity, receipt/timestamp, validation version, HTML and artifact references;
it has no foreign key to the deleted live outbox. Preexisting historical recovery
parents receive no invented parts. Failed archival rolls back replacement too.

Unknown final/notice delivery blocks later outboxes in that topic. If its job
remains result_ready, subsequent productive work also remains blocked. No current
control releases this delivery hold. Schema43 is a source prerequisite only:
activation is gated on a separately reviewed explicit owner reconciliation action.
This gate prevents turning a transport timeout into an irrecoverably stalled
deployed topic. Such an action must retain evidence without resend or provider
replay; converting unknown to delivered/failed is not a substitute.

## Verification and limits

Focused tests cover malformed/bool receipts, external/embedded/document/progress,
pre/post-fence expiry, commit faults, committed multipart prefix, cleanup faults,
deadline clocks, exact terminal observation, and populated schema42 migration
with triggers, deferred-FK failure and DDL rollback after old-child deletion.
Opener-level form/multipart tests distinguish strict Telegram HTTP400/403/429
rejections from proxy/malformed/conflicting bodies, HTTP408/5xx and incomplete
HTTP200 responses. Canonical validation, exact-candidate independent reviews and hosted checks are
required before publication completion. Telegram/native deployment acceptance is
separately authorized. This prerequisite supplies delivery evidence for future
owner assessment; it implements no assessment command, judge, scoring or advisor
workflow. Unknown delivery reconciliation remains explicit, never blind resend.
Before activation, a copy of the live database must pass foreign_key_check and
orphan-part inspection as well as the candidate migration gate. Existing FK
violations fail the upgrade closed and retain its consistent backup.
