# ADR 0108: Receipt-bound document artifact retirement

Status: accepted implementation decision; source publication and installation
remain pending.
Date: 2026-11-19.
Amends: [ADR 0060](0060-final-and-progress-delivery-certainty.md) and
[ADR 0069](0069-late-receipts-and-notice-ordering.md).

## Context

Hub keeps a private immutable spool copy of a result document until a positive
Telegram receipt for that document part is committed.  Cleanup then removes the
spool object.  A later backup reader therefore sees an intentionally absent
path even though the original final outbox is terminally delivered.

Treating every absent delivered document as safe would turn a damaged or
unproven record into success.  Rewriting `delivered` to `unknown` would erase
meaningful delivery state and reopen FIFO/recovery behavior.  Existing legacy
records also cannot gain modern receipt provenance merely because they retain a
Telegram ID.

## Decision

1. Schema 58 adds append-only `telegram_artifact_retirements`.  One record is
   keyed by the original outbox/document part and snapshots its exact
   immutable send-attempt ID and binding hash, Telegram message ID and receipt
   time, spool path/name/size/SHA-256, and terminal-retirement time.
2. `DeliveryReceipts` creates records only inside the existing terminal final
   receipt transaction, after every final part has projected and before the
   caller can clean a spool file.  An `unknown` outbox and a delivered multipart
   prefix create no record.
3. The code rederives the current final-part binding and requires exactly one
   latest immutable attempt/receipt pair.  SQLite guards independently require
   terminal `delivered`, a v1 part receipt, matching snapshot fields and the
   same final attempt/receipt.  Tombstones, their source part fields and the
   relevant live outbox fields cannot be updated or replaced through ordinary
   state writes.
4. Backfill is deliberately narrow: only already terminal v1 document parts
   with the same exact immutable evidence receive a record.  Legacy v0 rows,
   missing ledgers and inconsistent bindings remain unretired blockers.
5. A normal certain-notice archive may delete its live outbox after copying its
   existing archive snapshot.  The retirement record deliberately has no live
   outbox foreign key, so it keeps its self-contained proof and immutable
   transport parent after that archive operation.

`retired_at` records the terminal outbox receipt time, not a claimed filesystem
unlink timestamp: historical cleanup time is not recoverable evidence.

## Consequences

- A consumer may use a retirement record only through its own supported
  compatibility path and only after verifying the record with the retained
  immutable attempt/receipt evidence.  This migration alone does not change a
  backup consumer, establish a successful backup, or authorize deployment.
- Schema 58 is additive but requires an artifact that can read schema 58 for
  ordinary runtime rollback.  Restoring an old database after new work is not a
  rollback path.
- The ordinary trust model still does not claim protection from a privileged or
  same-UID actor that can forge the whole local database.  The ledger prevents
  accidental or product-path mutation; it is not an external signature.
- The migration and its evidence backfill share the established `BEGIN
  IMMEDIATE`, SQLite-consistent pre-migration backup, integrity/FK checks and
  rollback-on-fault boundary.

## Verification and rollout

Offline tests cover terminal-before-cleanup ordering, unknown and multipart
refusal, late-receipt/restart projection, exact-only migration backfill,
snapshot mismatch/refusal, source/tombstone immutability, archive retention,
and DDL/backfill rollback.  They use fictional SQLite and Telegram transports;
they do not establish installed state, foreign-consumer compatibility, a backup
run, or live Telegram acceptance.

Before any activation, publish a reviewed artifact with a schema-58-compatible
rollback, update the owned consumer separately, take a fresh consistent backup,
and use the normal admission/drain/readback procedure.  No retry of an earlier
ambiguous backup follows from this source change.
