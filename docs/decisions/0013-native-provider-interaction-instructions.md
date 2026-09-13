# ADR 0013: Native provider channels for Telegram interaction instructions

Status: accepted; Codex phase implemented, other provider capability checks planned
Date: 2026-09-05

## Context

The first Telegram interaction contract wrapped stable product instructions and
the current user request into one provider prompt. That is a safe compatibility
fallback, but it gives product policy the same apparent role as user content and
makes prompt-presence tests look stronger than the user-visible evidence they
actually provide.

Codex app-server exposes `developerInstructions` on both `thread/start` and
`thread/resume`. The contract can therefore use a provider-native instruction
channel without changing the user's request or weakening the existing sandbox,
approval, session, and one-writer boundaries.

## Decision

1. Telegram Interaction Contract v2 separates stable semantic instructions from
   per-turn transport metadata and user content.
2. Codex receives the full contract or compact reminder through
   `developerInstructions` on every Hub-owned `thread/start` and
   `thread/resume`. The exact user request and bounded per-turn artifact staging
   directory remain turn input because the directory changes for every job.
3. A session acknowledges v2 only after a successful productive turn. A session
   that acknowledged v1 receives the full v2 contract once before later compact
   reminders.
4. A provider without a verified native instruction/profile interface keeps the
   existing bounded prompt fallback. OpenCode and Antigravity move only after
   separate capability and behavioral acceptance; this decision does not claim
   parity.
5. Contract wiring tests remain necessary adapter evidence, but acceptance
   requires provider-specific behavioral scenarios for short, ambiguous, long,
   and artifact-producing tasks.
6. The Hub continues to own Telegram UI effects and durable delivery. Provider
   instructions cannot send or prove a draft, button, attachment, message part,
   reaction, or delivery receipt.

## Consequences

- Codex sees the interaction policy at the intended instruction priority while
  the productive request remains identifiable as user input.
- Existing provider sessions upgrade without reset or transcript injection.
- The fallback remains explicit and removable per provider instead of becoming
  a permanent provider-neutral abstraction.
- Behavioral E2E and the OpenCode/Antigravity capability checks remain required
  before the v2 checkpoint is accepted across the configured provider set.
