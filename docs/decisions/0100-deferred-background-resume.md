# ADR 0100: Hub-owned background work with automatic resume (deferred)

Status: deferred by owner decision, 2026-11-11. Next review: after one week of
ADR 0098 telemetry, or earlier on the reopening event below.

## Context

A Hub turn ends with its reply and cannot keep background work, so agents wait
for long checks inside the turn. After ADR 0098 the one-hour prompt cache makes
such waits cheap in tokens; the remaining cost is that the topic stays busy and
the owner sends "continue" when a long check outlives the turn.

## Considered

- **A (kept):** agents wait with one bounded command, or end the turn stating
  what runs and how to check it; the owner continues the conversation.
- **B (deferred):** the agent requests a Hub-owned transient unit; when it exits,
  Hub queues one follow-up turn to the same session with the result. Open design
  problems: a running gate or deployment must keep the project root, so the
  topic is not really free; launch and persistence must be atomic with
  deterministic unit names; `/stop` and `/new` need fenced cancellation at every
  transition; status and output must survive restarts; unit output is untrusted
  data, not Hub authority; resumes need durable budgets. It needs a schema
  migration and a new execution path.
- **C (rejected):** native CLI background tasks; they break exact `/stop` and
  leave processes without a Hub owner.

## Decision

Keep A. Revisit B when measured queue latency, blocked-root time, manual
continuations or missed completions show recurring owner pain, and only with a
fault-injection prototype that proves bounded cost, fenced cancellation and
restart-safe reconciliation. Owner: repository owner.
