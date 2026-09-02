# ADR 0006: Headless Codex fallback cannot wait for unreachable approvals

Status: accepted
Date: 2026-09-02

## Context

The preferred shared Codex app-server socket can have a companion client such
as tlive that renders and answers `on-request` approvals. The official stdio
fallback is a private child process of one provider worker. A companion attached
to the shared socket cannot see server requests emitted on that private stdio
stream.

Leaving such a request unanswered is fail-closed with respect to authority, but
it can hold the only Codex execution slot until the transport timeout. That
strands unrelated Codex topics and gives the user no actionable approval card.

## Decision

- Shared companion-capable sockets retain `workspace-write` plus `on-request`.
- An isolated stdio fallback retains `workspace-write` but pins
  `approvalPolicy: never`. Sandboxed work remains available; escalation does
  not.
- If the app-server nevertheless emits a command or file approval request, the
  fallback client answers `decline`. Permission requests receive an empty grant,
  elicitation is declined, and unsupported server requests receive a bounded
  JSON-RPC error.
- The fallback never converts an unavailable approval channel into approval and
  never expands filesystem or network authority.

## Consequences

- Optional multi-auth or shared-socket failure no longer turns a safe fallback
  into an hour-long invisible approval wait.
- A fallback turn that genuinely requires escalation may finish with a visible
  limitation instead of completing the privileged action. Restoring the shared
  companion-capable path is the recovery action.
- A turn already waiting inside an older stdio process cannot be repaired by a
  code update without interrupting an outcome that may already be ambiguous;
  the new behavior applies when a fresh fallback client is created.
