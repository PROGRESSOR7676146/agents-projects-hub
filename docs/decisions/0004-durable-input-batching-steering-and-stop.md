# ADR 0004: Durable input batching, provider steering, and emergency stop

Status: accepted

## Context

Telegram users commonly send one instruction as several consecutive messages.
Treating each message as a complete provider turn can start work from an
incomplete first fragment, duplicate shared context, and leave later fragments
waiting behind the wrong work. Inputs can also arrive while a provider turn is
already running, and emergency cancellation must not depend on model attention.

## Decision

The Controller persists every admitted Telegram message exactly once. Compatible
productive messages remain collectable for a short quiet window and become one
provider job with ordered, explicitly separated parts. The first message owns
the context/handoff snapshot; later parts do not duplicate it. A bounded maximum
window, payload limit, command, or routing/session/model/effort change closes the
batch.

After execution starts, delivery is capability-aware:

- a socket-backed Codex app-server receives ready compatible successors through
  `turn/steer` with an expected active-turn precondition;
- explicit app-server rejection safely returns the successor to FIFO;
- transport ambiguity becomes `indeterminate`, never an automatic replay;
- providers without safe same-turn steering receive one coherent subsequent
  FIFO turn.

An exact emergency utterance is a local control event. It never enters a model
prompt. Hub durably records it, cancels queued/retryable work for the active
provider in that topic, and interrupts the running Codex turn or the provider
process owned by the isolated worker. Normal prose containing the same word is
not a stop request. For an external CLI, the emergency path force-terminates
only the process group created and owned for that provider turn, including a
provider that ignores graceful termination.

## Consequences

The system adds a small bounded admission delay, durable input-membership and
absorption records, and provider-specific control behavior. It preserves strict
topic order, crash ambiguity rules, provider identity, and the one-writer
invariant. Stdio-isolated Codex cannot be addressed by a second app-server
client for same-turn steering, so it uses FIFO follow-up; emergency stop closes
its owned private app-server process.

This is semantic input delivery, not terminal transcript mirroring. Models may
choose how to incorporate successfully steered input, but they cannot decide
whether Hub persists, orders, drops, or retries it.
