# ADR 0011: Explicit native session ownership transfer

Status: accepted; implemented for Codex, deployment acceptance required
Date: 2026-09-04

## Context

The owner alternates between a Telegram project topic and a provider's native
CLI. Simultaneous terminal mirroring is unnecessary. `tlive run` remains useful
for remote terminal access and approvals, but its wrapped PTY is not the desired
primary project interface and is not provider-neutral.

The existing `/local` and `/return` implementation already persists a writer
mode and provider session ID. Its return path additionally invokes the provider
to generate a summary. That invocation is unnecessary when both frontends
resume the same provider-owned session, spends tokens, can distort context, and
resembles an automatic handoff that the product otherwise rejects.

## Decision

Hub transfers exclusive writer ownership rather than a live process, terminal
screen, or transcript:

1. A transfer is allowed only after the current provider turn and all durable
   work for that topic have reached a terminal state.
2. `/local` changes ownership from `telegram` to `local` and supplies a reviewed
   native resume command containing the exact provider session ID and canonical
   project root.
3. While `local` owns the session, Telegram productive input is rejected before
   provider invocation.
4. The owner closes the native CLI and explicitly issues `/return`.
5. `/return` changes ownership back to `telegram` without invoking a model,
   synthesizing a summary, copying a transcript, or creating a handoff.
6. The next productive Telegram message resumes the same provider session.
7. Version one relies on the explicit owner assertion that the local CLI is
   closed. Generic process discovery, screen scraping, and automatic takeover
   remain out of scope. A reviewed optional launcher with a local lock may be
   added only if live use demonstrates recurring concurrent-writer mistakes.
8. Codex is the first acceptance target. OpenCode and Antigravity retain their
   existing minimal commands until their native resume behavior passes the same
   live acceptance contract. Hermes fails closed until it exposes a reviewed
   native resume contract.

This is same-session continuation, not inter-agent handoff. Prior dialogue is
not injected unless the user explicitly requests the separate bounded context
feature.

## Preconditions for implementation

- The current branch passes the complete validation gate and is protected in
  the remote repository.
- The deployed services run the validated revision and pass a bounded Telegram
  smoke check.
- A Codex feasibility canary proves Hub-created thread → native CLI resume →
  CLI close → Hub resume of the same thread without concurrent writers.
- Telegram transport errors are either resolved or shown to retry without lost
  or duplicated productive work.

## Consequences

- Native CLI UX remains native; Hub does not emulate a terminal.
- Return becomes deterministic, fast, and free of model cost.
- The initial contract cannot prove that an independently launched local CLI is
  closed. It fails closed on Hub-known active work and documents the explicit
  owner assertion instead of claiming OS-level certainty.
- tlive remains an independent recovery, monitoring, approval, and optional
  remote-terminal component. It is not required for Hub session transfer.
- Active-turn migration, simultaneous writers, message-by-message mirroring,
  and machine-loss recovery remain outside this decision.
