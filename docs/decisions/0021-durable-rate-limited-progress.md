# ADR 0021: Durable rate-limited progress delivery

Status: schema support implemented; delivery behavior pending
Date: 2026-09-12

## Decision

Schema 24 adds a separate `provider_progress_deliveries` queue tied to completed
visible assistant commentary items. Progress is distinct from the final-result
outbox: delivering it never completes a provider job, acknowledges context, or
authorizes replay. The provider identity sends progress only while the exact job
remains executing.

The first eligible commentary item may be queued immediately. Later items are
rate-limited per job, bounded to Telegram-safe text, and deduplicated by the
durable visible-item sequence. Pending progress is superseded when the job
becomes terminal so an old update cannot arrive after a final result or failure.
Telegram retry deadlines survive sender restart and use the same bounded retry
policy as final delivery.

## Migration and rollback

The additive table has its own leases and terminal states and shares the normal
transactional migration, backup, and integrity gate. Schema support is released
before delivery behavior so that revision can serve as the schema-24 rollback
artifact.

## Evidence

Migration coverage verifies the schema-23 to schema-24 boundary and complete
table shape. Behavior coverage must prove item deduplication, rate limiting,
provider identity, restart-safe retry, terminal supersession, and unchanged job
state after progress delivery.
