# ADR 0015: Telegram transport health threshold

Status: accepted and implemented
Date: 2026-09-05

## Context

Telegram polling and durable delivery already retain bounded failure class,
operation, status, retry-after, consecutive count, and last-success data. The
health projection nevertheless degraded on the first failure, while event
logging could treat a changed failure signature as a new edge during the same
outage. That made a routine transient request indistinguishable from a repeated
transport failure and weakened the meaning of recovery.

## Decision

1. Retain every current bounded transport failure in runtime health, including
   consecutive count, so the first two failures remain observable.
2. Degrade a required component and emit `telegram_transport_error` when the
   third consecutive polling or delivery failure occurs.
3. Emit no additional transport-error edge while that failure episode remains
   active, even if the safe failure signature changes.
4. A successful transport request clears the episode. Emit
   `telegram_recovered` only when the episode crossed the degradation threshold,
   then re-arm the threshold for a future independent episode.
5. Advisory chat actions retain their separate best-effort contract; they do
   not affect provider work or required-component health.

## Consequences

- One or two transient Telegram failures are visible diagnostic state but do
  not falsely degrade a live component.
- Three uninterrupted failures are a stable bounded signal of degraded
  transport, and monitoring still deduplicates the corresponding operational
  alert.
- Recovery and re-arm behavior is deterministic across Controller polling,
  standalone outbox delivery, and legacy direct-provider pollers.
