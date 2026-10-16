# ADR 0069: Late Telegram receipts and execution-notice ordering

Status: accepted design; source implementation under validation.
Date: 2026-10-16.
Owner: Hub maintainer; product decision owner: repository owner.

## Problem and decision

The send lease authorizes starting transport. Its expiry does not invalidate a
positive response from that exact already-started request. Previously, expiry
discarded that receipt and parked the outbox as unknown. An unknown failure
notice then blocked every later saved answer in the topic. Increasing the lease
would only postpone this failure.

This decision supersedes the late-receipt and unknown-notice FIFO policies of
[ADR 0060](0060-final-and-progress-delivery-certainty.md) and the corresponding
ordering rule of [ADR 0001](0001-durable-provider-job-queue.md).
[REQ-QUEUE-005](../product/DELIVERY_AND_RECEIPTS.md) owns the contract.
The exact owner controls of [ADR 0063](0063-delivery-control-reconciliation.md)
retain their existing authority and snapshots.

Schema54 adds an immutable attempt ledger and separate immutable receipt ledger.
The exact lease token identifies one send attempt. The fence and attempt commit
together before HTTP. The attempt binds kind, target, part, sender, numeric
destination, historical job/session generation and the payload/artifact snapshot
through a canonical SHA-256 fingerprint. A monotonic local sequence distinguishes
attempts even when wall clocks coincide. There is no backfill of send proof.

A strict positive non-boolean Telegram message ID first commits to the receipt
ledger. A second transaction may project it onto the unchanged original part and
outbox, even after stale recovery has parked that attempt. Receipt persistence
is idempotent for the same ID and refuses a conflicting ID. Bounded local-only
retry handles SQLite/I/O persistence faults; it never repeats HTTP. Restart
maintenance projects saved receipts without network or provider access. A crash
before the first durable receipt still leaves a genuine unknown send.

Projection refuses changed payload/destination/generation, newer attempts,
changed send ownership and existing owner dispositions. A late receipt may remain
archival evidence while the original owner-pinned delivery snapshot stays unknown.
Multipart prefix receipts and artifact references remain intact. Cleanup follows
only an actually projected exact part receipt; a cleanup fault cannot resend.
After a crash between receipt persistence and projection, recovery may retain a
delivered spool copy; no receipt permits deletion of an unprojected artifact.
Attempt ledgers deliberately have no FK to replaceable live notices: the existing
notice archive can replace a certain notice without deleting transport evidence.
Receipt-to-attempt references and immutable guards retain that evidence.
The receipt table has no rowid replacement path. Bounded restart pages advance
a local cursor so archival receipts cannot indefinitely hide later valid proof.

New outboxes carry immutable purpose `provider_result` or `execution_notice`,
assigned by the trusted producer in its existing outcome transaction. Migration
labels all historical outboxes `legacy`; absence of a result is not migration
proof of notice purpose. An unknown explicitly classified execution notice for a
terminal resultless job does not block later outbox delivery. Unknown real finals,
legacy records and all multipart ordering stay conservative. Native execution
uncertainty, root/writer exclusion, stops, owner holds and capacity remain
independent. Notice bypass grants no session/model/local-transfer authority.

Status separates saved results awaiting delivery, unknown blocking delivery and
unconfirmed nonblocking notices. Passive counters keep every unknown visible;
an independent deduplicated Operations warning reports nonblocking notices without
claiming delivery or invoking inference. No automatic resend, fabricated receipt,
new owner button or MTProto runtime dependency is introduced.

## Ownership and architecture review

HubState remains the sole transaction owner. DeliveryStateFacade owns the
orchestration of the two commits; the new dependency-neutral DeliveryReceipts
domain owns attempt binding, evidence SQL and projection. This extracts existing
receipt settlement from state_delivery rather than adding another lifecycle to
the Controller. Shared final/progress helpers own transport; receipt_commit owns
only bounded local retry. Provider invocation is outside this change.

Hotspot exception: Hub maintainer owns the small producer discriminator and DDL
registration changes in HubState, ObservedTurnResults and migrations. Scope is
only purpose assignment/registration; no additional transaction or native-call
responsibility enters those files. Mechanical extraction of their existing
transactions would increase coupling. Review by 2026-11-16, or immediately on
new producer policy, another purpose, ledger pruning or additional transport
authority. Independent exact-candidate Claude review remains required.

## Verification and rollout

Required fault coverage includes late response after four minutes, stale recovery
from a second connection, commit-then-raise, bounded SQLite retry, crash between
evidence and projection, conflicting IDs, mismatched bindings, multipart prefix,
owner consent, new-notice bypass, legacy/true-final blocking, progress and existing
native/root/stop guards. All fixtures are offline and invoke no model.

Migration retains legacy evidence and uses the existing SQLite-consistent backup,
upgrade transaction and failure rollback. Schema53 executables cannot open schema54.
Activation needs exact reviewed candidate and compatible rollback artifacts,
canonical/hosted checks, coordinated component convergence and separately scoped
Telegram acceptance. A database backup never permits erasing later accepted work.
The append-only ledgers inherit outbox evidence retention; introducing deletion
or a size cap requires a reviewed lifecycle/archival policy.

The delivery clause moves unchanged in identity from section22 into its own
section23 module to keep normative documents below the publication size bound.
Only the reviewed receipt and notice-ordering policy changes; the remaining
queue clauses and requirement IDs retain their meanings.
