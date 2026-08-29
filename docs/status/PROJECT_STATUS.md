# Project status

Status: active pilot  
As of: 2026-08-29  
Branch observed: `feat/provider-account-profiles`

This file is a concise implementation/evidence view. Product intent and full
acceptance criteria live in
[`docs/product/PRODUCT_REQUIREMENTS.md`](../product/PRODUCT_REQUIREMENTS.md).

## Implemented and previously verified

- Central ingress routes ordinary, Reply, mention, and quote cases without
  invoking non-target provider models.
- Per-topic bounded visible context is shared on another agent's next productive
  turn for Codex, OpenCode, Antigravity, and Hermes.
- Numeric project/topic identity, canonical root isolation, idempotency,
  persistent provider session IDs, active agent state, and writer state use
  SQLite schema v6 with pre-migration backup.
- Codex app-server, Hermes Gateway integration, OpenCode, and Antigravity
  adapters exist; local OpenCode and Antigravity provider probes passed before
  this documentation baseline.
- Codex terminal takeover/release uses a one-writer lease and tmux fallback.
- `codex-multi-auth` is optional; official Codex stdio is the fallback.
- Hub, Hermes Gateway, and tlive are diagnosed and monitored independently.
- Hub, Pythia, and Babelfish are registered as isolated real projects.
- The handoff records 112 passing tests plus Ruff and Pyright before this task.

## Planned next

1. Run the owner-driven Hub General live Telegram E2E baseline described in the
   latest handoff and product acceptance criteria.
2. After review and E2E acceptance, implement minimal `/local` and `/return`.
3. Add bounded `/publish` for local-work summaries.
4. Build and restore-drill an encrypted disaster-recovery bundle.

## Deferred

- Automatic Antigravity account rotation pending a stable supported headless
  pool interface.
- Provider-neutral Session Bridge.
- Full provider parity for semantic remote companions.
- Additional providers and removal of tmux fallback.

## Rejected from the current design

- Automatic terminal window/PID orchestration and TUI screen scraping.
- Message-by-message native CLI transcript mirroring.
- Automatic approval, approval by Hermes, or security relaxation during repair.
- Telegram-selected filesystem paths or silent project rebinding.

## Known limitations and blockers

- The Hub General central-ingress E2E has not been run under this baseline.
- The Codex bot can send messages/documents in Hub General but lacks Manage
  Topics, so it cannot create additional forum topics.
- Exact in-flight provider turns cannot be recovered after process or machine
  loss; only completed persisted state is recoverable.
- Machine-loss recovery tooling is planned rather than implemented.

## Status update rule

Change a capability to implemented only when repository behavior and relevant
tests support it. Mark live acceptance separately when it requires real owner
messages, external accounts, service restarts, or Telegram identity checks.
