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
