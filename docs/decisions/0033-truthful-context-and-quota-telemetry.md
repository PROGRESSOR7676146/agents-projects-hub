# ADR 0033: Truthful context and quota telemetry

Status: accepted  
Date: 2026-09-20

## Context

Codex app-server reports both `last` and `total` token usage. `last` describes
the current context snapshot and can decrease after compaction; `total` is
cumulative session usage and can exceed the model context window. Treating the
cumulative value as current occupancy produced a false `0%` remainder.

Rate-limit snapshots identify windows as `primary` and `secondary`, but those
positions do not define their duration. Existing presentation called them
five-hour and weekly even when upstream reported another duration. Passive pool
snapshots also discarded duration, so cached views could not label them
truthfully.

## Decision

- Compute Codex context remainder only from the latest valid
  `last.totalTokens` and `modelContextWindow` pair for the turn.
- Let a later snapshot replace an earlier one so compaction is represented.
  A successful turn without a valid current-context snapshot stores unknown
  rather than retaining or inventing a percentage.
- Carry provider-reported window duration through the bounded passive account
  snapshot. Build every user-visible response, status, account, rotation and
  alert label from that duration.
- Use `Primary window` and `Secondary window` when duration is absent. Position
  alone never implies a tariff period.
- Continue to label stale account quota as cached and suppress stale low-quota
  alerts. Passive monitoring does not invoke a model.

The existing nullable session context column is sufficient, and the bounded
pool snapshot is not durable product state. No database migration is required;
the target schema remains 33.

## Consequences

Context display follows compaction and no longer collapses to zero from
lifetime token spend. Window labels remain correct for 15-minute, five-hour,
weekly and future durations, including a weekly primary-only response. Legacy
cached snapshots without duration remain readable but intentionally receive a
generic positional label.
