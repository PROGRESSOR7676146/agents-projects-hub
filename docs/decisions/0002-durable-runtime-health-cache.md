# ADR 0002: Durable runtime health cache

Status: accepted  
Date: 2026-08-30

## Context

The Controller, Telegram sender, and provider workers must remain independently
observable when a provider is unavailable. A health command that invokes a
model or provider API would reproduce the dependency it is meant to diagnose,
consume quota, and make local control-plane status fail with the provider.

Process existence alone is also insufficient. A process may be alive while its
event loop is stalled, and a recycled PID must not be confused with the
previous process instance.

## Decision

Runtime components publish small last-known snapshots to the existing private
SQLite state database. Migration 12 adds one row per `(component, instance_id)`
with bounded fields only:

- component, instance, runtime, and agent identity;
- PID plus a per-process-start marker;
- start, heartbeat, last-success, lease-expiry, and quota-reset timestamps;
- a bounded error code, activity state, and active job identifier;
- constrained provider state and optional quota percentage.

The cache does not contain exception text, prompts, responses, command lines,
environment data, credentials, account identifiers, or provider payloads.

Readers classify cached rows as `healthy`, `degraded`, `stale`, or `unknown`
using configured age thresholds. Provider limits and bounded runtime errors
make a fresh row degraded. Classification performs no network request, process
probe, provider call, or model turn.

Provider workers publish lifecycle, lease, success, and provider-limit state in
this slice. Controller and sender lifecycle integration remain separate work;
their rows use the same schema and API.

## Consequences

- Status remains available from deterministic local state during provider
  failure and does not spend model tokens.
- A stale row is evidence that reporting stopped, not proof of the precise
  process failure; recovery code may perform a separate bounded process check.
- Each process must use a stable instance ID and a new start marker on restart.
- Health publication is best effort and must never prevent productive queue
  work. Monitoring treats missing publication as `unknown`, then reports it
  explicitly rather than assuming health.
