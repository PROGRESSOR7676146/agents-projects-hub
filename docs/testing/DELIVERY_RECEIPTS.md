# Late receipt and notice-ordering fixtures

`tests.test_late_delivery_receipts` and `tests.test_delivery_receipt_migration`
cover late responses, local-only commit retry, durable proof before failed
bookkeeping, bounded restart reconciliation, changed bindings/new attempts,
immutable evidence including rowid replacement, owner snapshot races, shared
progress and new-notice ordering. Populated53 backups and injected DDL failure
retain historical legacy purpose. These are offline source and fault tests,
not installed Telegram acceptance.

The [delivery contract](../product/DELIVERY_AND_RECEIPTS.md) owns behavior.
The [testing guide](README.md) owns publication commands and acceptance boundaries.

`tests.test_artifact_loss` covers schema-59 local-only loss attestation and
the native document recovery inventory. It uses fictional closed snapshots and
spools to prove atomic batch CAS, exact retry, concurrent owner refusal,
mode-0600 selection input, schema-58-to-59 DDL rollback, stale bindings,
reappeared files, malformed ledger refusal and the exact legacy `unknown` v1
receipt boundary. A `degraded_explicit_loss` fixture is deliberately not a
backup, archive, installation or Telegram acceptance claim.
