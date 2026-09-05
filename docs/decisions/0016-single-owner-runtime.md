# ADR 0016: Single-owner runtime and private Codex transport

Status: accepted
Date: 2026-09-05

## Context

ADR 0001 introduced separate Controller, provider-worker, and outbox-sender
processes to maximize provider failure isolation. The product is deployed for
one owner on one machine. In that setting the extra units, revision convergence,
credential ownership, startup ordering, and rollout gates cost more operational
attention than the additional process isolation returns.

The Hub Codex worker also preferred the same shared app-server socket watched by
the tlive Codex companion. Consequently project sessions appeared in Agent
Session Remote, where reply and approval controls are intended for interactive
Codex sessions rather than Hub-owned work.

## Decision

The recommended deployment is one Controller process configured with:

```json
{
  "dispatch_mode": "queue",
  "queue_runtime": "embedded",
  "outbox_runtime": "controller",
  "codex_transport": "stdio",
  "codex_stdio_executable": "/home/example/.local/bin/codex"
}
```

The SQLite queue, outbox, idempotency, strict topic FIFO, writer leases, and
conservative `indeterminate` handling remain unchanged. Provider work runs on
the existing background consumer, not on the Telegram polling thread. The same
process owns central ingress, local provider adapters, and their response
transports. The deterministic monitor timer may remain separate because it does
not invoke a model.

Explicit `stdio` always ignores the shared socket and starts the official Codex
app-server privately. It uses `workspace-write` with `approvalPolicy: never`;
unexpected approval requests are declined. tlive continues to watch the shared
socket for interactive Codex sessions but cannot observe the private Hub
transport.

External provider workers plus the standalone sender remain a supported opt-in
topology. Selecting it still requires every locally managed provider to have a
worker and keeps provider credentials out of the Controller.

## Consequences

- The routine deployment shrinks from Controller, sender, and one worker per
  local provider to one Controller service.
- There is one revision and one main process to start, stop, inspect, upgrade,
  and roll back.
- A Controller crash briefly pauses all local providers, but systemd can restart
  it and durable state survives. For a one-owner installation this is an
  accepted trade-off.
- Hub Codex loses transparent shared multi-auth rotation in private stdio mode.
  Account selection is delegated to the official Codex credential active for
  that process; multi-auth remains optional for interactive clients.
- Direct-provider private-message units are not required for project groups and
  should be disabled when the owner does not use them.

## Supersession boundary

This record supersedes ADR 0001 only where it treated external workers and a
standalone sender as the intended final topology. ADR 0001 remains normative for
durable queue state, idempotency, ordering, retry, delivery, and ambiguity.

After the live cutover and smoke acceptance, feature development enters a
maintenance freeze. Further work is limited to defects, security, provider
compatibility, and explicit operator-requested changes.
