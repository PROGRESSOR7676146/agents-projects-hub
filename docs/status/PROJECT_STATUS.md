# Project status

Status: active pilot  
As of: 2026-08-30
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
  SQLite schema v9 with pre-migration backup; the last known context remainder
  is persisted per provider session for compact status output.
- Codex app-server, Hermes Gateway integration, OpenCode, and Antigravity
  adapters exist; local OpenCode and Antigravity provider probes passed before
  this documentation baseline.
- Codex terminal takeover/release uses a one-writer lease and tmux fallback.
- `/local` and `/return` explicitly transfer one-writer ownership between
  Telegram and native Codex, OpenCode, or Antigravity CLIs without launching or
  scraping terminals; `/return` also publishes a bounded local-interval summary.
  Hermes fails closed pending a native resume contract.
- The compact control surface provides `/status`, a single provider → model →
  effort `/model` menu, `/accounts`, provider-neutral `/new`, `/local`, and
  `/return`. Provider catalogs are locally validated and Antigravity has a
  bounded fallback catalog if its discovery probe is unavailable.
- Codex account rotation telemetry is event-driven from upstream provider `429`
  counters. Notifications always target Hub Operations and additionally target
  a work topic only when exactly one Codex topic is active.
- OpenCode Go records exact reset telemetry from provider `429` responses and
  otherwise labels only static plan caps; provider failures are isolated and do
  not crash central ingress.
- Telegram accepted and returned the exact public command menu (`/status`,
  `/model`, `/accounts`, `/new`, `/local`, `/return`) for the locally managed
  Codex, OpenCode, and Antigravity bot identities. Live catalog probes returned
  25 OpenCode Go models and 7 grouped Antigravity model families.
- `codex-multi-auth` is optional; official Codex stdio is the fallback.
- Hub, Hermes Gateway, and tlive are diagnosed and monitored independently.
- Hub, Pythia, and Babelfish are registered as isolated real projects.
- Hub General passed live owner-driven E2E for ordinary routing, satellite
  mention, Reply-to-author, shared context, idle-provider non-invocation,
  provider identity, and controlled-restart continuity.
- Hermes project-group policy, gateway heartbeat, Bot API, and pending-update
  queue are monitored; cooldown-bounded repair can sync missing registered
  groups and restart only Hermes Gateway.
- Operational alerts have one explicit destination: Hub topic
  `Operations · Alerts` (thread `41`). Codex is primary and Hermes may fall back
  only to that same topic. Quota alerts contain a masked account identity.
- The live evidence is recorded in
  [`docs/testing/2026-08-29-hub-general-live-e2e.md`](../testing/2026-08-29-hub-general-live-e2e.md).

## Planned next

1. Complete owner-driven click-through E2E for provider/model selection and a
   natural or controlled Codex quota transition. Telegram menu readback and
   real provider catalog discovery already passed.

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

- The Codex bot can send messages/documents in Hub General but lacks Manage
  Topics, so it cannot create additional forum topics.
- Exact in-flight provider turns cannot be recovered after process or machine
  loss; only completed persisted state is recoverable.
- Machine-loss reconstruction remains manual and is not in the current plan.

## Status update rule

Change a capability to implemented only when repository behavior and relevant
tests support it. Mark live acceptance separately when it requires real owner
messages, external accounts, service restarts, or Telegram identity checks.
