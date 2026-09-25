# ADR 0036: Durable local-root blockers

Status: repository implementation; live acceptance pending
Date: 2026-09-25

## Context

A local session may retain the exclusive writer lease after its CLI closes.
Another topic on the same canonical root could previously accept a productive
request into a queue that would not advance. A later `/return` could then run
that old request without a fresh owner decision. Age and process visibility
cannot prove that the local writer has stopped.

## Decision

Schema 35 adds a durable Hub blocker outbox and extends the existing job-hold
table with reason and owner decision. Admission checks the exact execution
scope in SQLite. A new request behind a persistent local or terminal writer,
or unresolved execution, gets one message-bound refusal and no provider job.
The reply links to the owner topic by numeric identity and uses a neutral
label; cached topic names are not trusted as current titles.

Previously accepted queued jobs remain in history. The sender discovers and
holds them before notification; `/return` holds any remaining jobs in the same
transaction that releases ownership. A held job remains paused until its
delivered, exact-job notice receives an owner confirmation or cancellation.
Confirmation is accepted only after the root is free. Pending holds preserve
FIFO; the explicit failed-turn continuation remains the narrow exception.
The sender retries only Telegram delivery. No status or blocker diagnosis
calls a model. `/status` is owner context; passive aggregates stay anonymous.

The owner closes the CLI before `/return`; Hub does not infer closure from a
PID or elapsed time. Releasing the lease keeps the same provider session and
does not classify old uncertain work or replay a prompt. The user-facing
notice routes to the owner topic instead of performing a cross-topic return.

## Ownership and recovery

HubState owns every SQLite transaction, including admission disposition, hold
creation, job decision and writer return. The Controller owns ingress and
callback authorization. The standalone Hub sender owns notice delivery and
its bounded retry lease. A send may have succeeded immediately before sender
loss, so one duplicate notice is possible; it never causes a second provider
invocation. Schema 34 executables cannot open schema 35 after migration.
Rollback therefore requires a schema-35-compatible release, not a database
downgrade. Offline tests use fictional SQLite, workers and Telegram transports;
live Telegram/provider acceptance is separate.
