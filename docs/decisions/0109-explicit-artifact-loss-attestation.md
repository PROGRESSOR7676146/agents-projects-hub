# ADR 0109: Explicit artifact-loss attestation and degraded recovery coverage

Status: accepted policy; source candidate, installation pending.
Date: 2026-11-19.
Amends: [ADR 0108](0108-receipt-bound-document-artifact-retirement.md).

## Context

An absent Hub-spooled document has two materially different explanations:

- an exact receipt proves that Telegram accepted it and an immutable retirement
  record proves why normal cleanup may have removed the spool copy; or
- the file is genuinely unavailable to recovery.

The second case must not be relabelled as delivery success, receipt evidence, or
a healthy backup.  It nevertheless needs a durable, bounded way to record the
owner's conclusion after recovery work, so repeated backup attempts do not
silently rediscover the same already-accounted-for loss.

## Decision

1. Schema 59 adds an append-only local-owner artifact-loss ledger.  An operation
   binds an exact batch of parked document outbox parts, their current final-part
   bindings and artifact SHA-256/size, a caller-supplied private recovery-audit
   digest, the preceding ledger head and a deterministic commit hash.
2. The supported write interface is a local preview/apply compare-and-swap.  Apply
   requires a private mode-0600 batch-selection file, an exact preview
   snapshot, an operation UUID and explicit acknowledgement of irrecoverable
   loss.  It has no Telegram callback, configuration, credential, provider,
   queue, delivery, hold, writer or lifecycle side effect.
3. A declaration is permitted only for an absent, exact document in a parked
   `delivered`, `unknown`, or policy-exhausted `failed` outbox without a
   receipt-retirement record.  Existing source artifact identity is frozen.
   A later positive receipt may still settle delivery; a valid retirement then
   takes precedence for recovery classification.
   The ordinary recovery-notice archive may remove that source only after it
   records an immutable tombstone containing the complete frozen final-binding
   inputs, parked delivery state, item identity and exact archived-copy
   reference.  The archive and tombstone are then immutable; a live source and
   tombstone together, a missing source without one, or any archive mismatch
   fails closed.
4. The native read-only inventory verifies a closed SQLite snapshot, exact
   receipt retirement, narrow legacy `unknown` v1 receipt evidence, complete
   loss-ledger chain and current source binding.  It reports each document as
   `present_required`, `receipt_retired`, `receipt_verified_unknown`,
   `explicit_loss`, or `unresolved_missing`.
5. `explicit_loss` produces `degraded_explicit_loss`, never complete recovery
   coverage.  A reappeared file is required again even though its historical
   declaration remains.  Any unsafe file, stale binding, malformed/partial
   ledger, missing table or unverified receipt fails closed.

The ledger is a **local owner attestation**, not a Telegram receipt, signature,
or proof that a private recovery audit occurred.  The trusted-host model does
not claim to resist a same-UID actor forging a whole SQLite database.  The chain
and exact schema-59 DDL comparison detect accidental or incomplete product-path
records, including retained no-op guards; an actor that can rewrite both data
and canonical DDL remains outside this boundary. An external signing key would
be a separate trust-boundary decision.

## Consequences

- A backup consumer may retain and report a degraded set, but must not call it a
  complete recovery set or use it to satisfy an update precondition that
  requires recoverable artifacts.  Any exception belongs to the owning update
  policy, is explicit and scoped, and is not inferred from this ledger.
- Existing schema-58 retirement compatibility remains unchanged.  Its exact
  receipt proof is distinct from loss attestation and remains preferred.
- Normal operation cannot delete, replace, re-order or mutate a ledger record.
  An uncertain exact retry validates the entire ledger before returning its
  original receipt; concurrent previews serialize through the current head.
- A recovery archive no longer deadlocks on a declared-loss source.  Its
  tombstoned document remains in the read-only inventory and is still
  `degraded_explicit_loss` until an identical spool file reappears; archival is
  not a way to erase loss evidence or make a backup update-ready.
- Schema 59 is additive.  Runtime rollback needs an artifact that reads schema
  59; restoring an old state database after newer work is not a rollback path.
  Its fixed release capability retains the reader and inventory but refuses a
  new loss declaration, so rollback cannot extend the owner-attested history.
  This binds the distinct rollback release of a migration. A same-schema
  deployment rolls back to the previously installed release with its own
  capability (ADR 0094 amendment, 2026-11-21).

## Verification and rollout

Offline fixtures cover batch atomicity, stale previews, exact retry,
concurrent writers, DDL rollback, partial/forged ledger refusal, file
reappearance, schema-58 fallback and exact legacy `unknown` v1 receipts.  They
use fictional state and prove neither an installed migration nor a recoverable
backup or Telegram delivery.

Before activation, review the exact source, provide a schema-59-compatible
runtime rollback, take a fresh consistent backup, update the owning archive
consumer, and perform the ordinary admission/drain/readback procedure.  Do not
replay an earlier ambiguous backup because this source path exists.
