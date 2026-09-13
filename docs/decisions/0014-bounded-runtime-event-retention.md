# ADR 0014: Bounded runtime-event retention

Status: accepted and implemented
Date: 2026-09-05

## Context

`runtime_events` is a diagnostic history, while current component health,
alert-transition state, runtime counters, and durable provider work live in
separate tables. Reads were limited to the newest 50 events, but writes never
removed old rows, so a long-running installation could grow without bound.

Retention must be deterministic, safe during schema upgrade, and unable to
acknowledge, retry, or otherwise mutate provider work. A count-only limit loses
the time horizon during a burst; an age-only limit still permits unbounded
growth during a fault storm.

## Decision

1. Keep events no older than 30 days and at most the newest 10,000 rows.
2. Order count retention by `created_at DESC, event_id DESC`; the monotonic row
   ID is the tie-breaker when timestamps match. An event exactly on the age
   boundary remains eligible.
3. Every API event insertion and both retention deletes share one SQLite
   transaction. A pruning failure rolls back the new event as well as every
   partial delete.
4. Schema version 21 replaces the single-column timestamp index with the
   composite retention index and applies the same age/count bounds to existing
   rows. The whole version transition, including `user_version`, runs in one
   `BEGIN IMMEDIATE` transaction. A fault rolls back in place instead of
   copying an older backup over a database that may have accepted a concurrent
   committed write.
5. Retention addresses only `runtime_events`. `runtime_health`, alert delivery
   and transition checkpoints, runtime counters, provider jobs, results, and
   outbox state are outside the deletion query.

## Consequences

- Diagnostic event history has a predictable storage ceiling and time horizon.
- Current health and alert state survive event pruning independently.
- The migration may intentionally discard expired or over-limit diagnostic
  history, while its pre-migration SQLite backup remains available to the
  operator.
- Temporary production-shaped rehearsal covers an open concurrent writer,
  injected DDL failure, the schema-20 backup, queued/result/outbox work, and an
  indeterminate job. It never opens deployment state.
- Status output remains capped at its existing newest 50 events.
