# Project status

Status: active alpha
Release: v0.5.0

This file describes repository capabilities only. It intentionally contains no
operator deployment inventory or live conversation evidence.

## Implemented

- Numeric project/topic identity, canonical allowlisted roots, idempotent
  routing, persistent provider sessions, bounded visible context, and writer
  leases backed by versioned SQLite migrations.
- Additive durable provider-job, result, and Telegram-outbox schema with atomic
  idempotent enqueue, strict per-topic FIFO leases, conservative stale-job
  recovery, and a feature-gated embedded compatibility consumer. `dispatch_mode`
  defaults to `inline`; `queue_runtime` defaults to `embedded`.
- An isolated Codex worker is available behind `dispatch_mode: "queue"` and
  `queue_runtime: "external"`. It owns only Codex app-server/SQLite execution,
  writes results and outbox rows, and never receives Telegram credentials or
  sends Telegram. The controller does not own a Codex supervisor in this mode;
  its temporary deterministic outbox loop delivers prepared Codex rows. Other
  providers remain on the embedded compatibility consumer pending their own
  workers. Controller status/account commands do not invoke `codex-multi-auth`
  in external mode; durable worker telemetry is planned with monitoring. The
  worker-specific loader does not open Telegram token files. No live queue
  cutover is implied by this repository change. Signal wiring, bounded shutdown,
  and managed-socket ownership guards remain part of the service/recovery stage.
- Central Telegram ingress with deterministic ordinary, Reply, mention, and
  quote routing; non-target providers are not invoked merely to observe.
- Codex app-server, Hermes Gateway integration, OpenCode, and Antigravity
  adapters with isolated failure boundaries.
- Compact `/status`, `/accounts`, cached and paginated `/model`, confirmed
  single-session `/new`, `/local`, and `/return` controls.
- Private last-known-good provider catalogs with bounded callback keys and
  stale-after-failed-refresh monitoring.
- Event-driven Codex quota rotation telemetry and provider-supplied OpenCode
  reset telemetry.
- Declarative Telegram command-menu synchronization.
- Sandboxed Antigravity `accept-edits` mode; dangerous permission bypass is
  rejected.
- Optional `codex-multi-auth` with official Codex stdio fallback.
- Independent Hub, Hermes Gateway, and tlive diagnostics and monitoring.
- Privacy gate that rejects deployment identities, raw histories/session dumps,
  owner-specific paths, Telegram secrets/identifiers, and local runtime files.

## Acceptance still required per deployment

- Owner-driven provider/model/effort click-through.
- Natural or controlled Codex quota transition.
- Telegram privacy/admin policy, restart continuity, and reply provenance after
  any material provider or routing upgrade.

Live acceptance results belong in private operational records, not this public
repository.

## Deferred

- Automatic Antigravity account rotation pending a stable supported headless
  pool interface.
- Provider-neutral Session Bridge.
- Full provider parity for semantic remote companions.

## Rejected

- Automatic approval or sandbox relaxation.
- Telegram-selected filesystem paths or silent project rebinding.
- TUI screen scraping and message-by-message CLI transcript mirroring.
