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
- Isolated queue workers are available for locally managed Codex, OpenCode, and
  Antigravity behind `dispatch_mode: "queue"` and `queue_runtime: "external"`.
  `external_worker_agent_ids` selects each isolated provider independently and
  defaults to `codex`, preserving rollback and embedded compatibility for every
  other provider. Each worker owns only its adapter/process and SQLite execution,
  writes results and outbox rows, and neither reads Telegram credentials nor
  sends Telegram. `outbox_runtime` defaults to the stage-5 `controller` delivery
  path for safe rollback. When explicitly set to `external`, a standalone fair
  outbox sender owns every locally managed queue agent's
  Telegram identities and durable delivery retries; it has no provider adapter
  or RPC capability. The controller neither constructs adapters for isolated
  agents nor delivers their prepared rows. Direct-message provider services remain
  separate endpoints. Hermes remains externally managed
  and out of worker scope. Controller status/account commands do not invoke
  `codex-multi-auth` when Codex is isolated. No live queue cutover is implied by
  this repository change. Controller, direct-provider, worker, and sender
  processes handle `SIGTERM`/`SIGINT` as stop requests, use bounded Telegram
  polling and cleanup joins, release work when stop is observed before
  invocation, and preserve work past the final cooperative boundary for
  conservative ambiguity recovery. Managed-socket ownership guards remain part
  of the service/recovery stage.
- Additive SQLite runtime-health cache and bounded state APIs cover Controller,
  sender, and provider-worker identities. Classification is derived only from
  cached heartbeat/error/provider state and never calls a model or provider.
  Controller, standalone Telegram sender, and external provider workers publish
  startup, heartbeat, activity, bounded error, and success snapshots as
  applicable. Local `status` projects every configured component from this cache,
  and monitoring distinguishes unknown, stale, and degraded components without
  probing their provider. General notifications retain the single configured Hub
  Operations destination.
- Central Telegram ingress with deterministic ordinary, Reply, mention, and
  quote routing; non-target providers are not invoked merely to observe. An
  explicit Codex mention while another provider is active uses a satellite
  Codex session and does not silently change the active provider.
- Providers declared `managed_externally` retain their native admission path
  and are never enqueued into the local worker queue, preventing accepted jobs
  without an eligible consumer. All-external productive routes remain unclaimed
  by Hub, mixed routes admit only their locally managed target set, and model
  menus use cached/configured data without invoking the external runtime. Codex
  remains the locally managed primary provider. Startup detects nonterminal
  queue rows left by a prior ownership configuration and fails visibly until
  they are drained or explicitly reconciled.
- Optional separate Hub Telegram controller identity for project-group ingress,
  commands, callbacks, and menu ownership. It persists a distinct `hub` update
  offset, keeps Codex as the default productive provider, and does not replace
  provider response/outbox identities. The controller loader opens only the Hub
  token when configured, or only Codex's token for legacy ingress; omitted
  `hub_bot` configuration preserves the prior Codex controller behavior.
- Telegram token files are validated as a single credential rather than merely
  containing a colon, so labels or copied surrounding text fail preflight. The
  operational timer schedules its first run relative to activation and every
  later run relative to the monitored unit, including a post-cutover start.
- A reusable fictional subprocess fault matrix exercises Controller admission,
  durable SQLite handoff, isolated external workers, and standalone outbox
  delivery together. Parent tests terminate child actors after enqueue but
  before offset persistence, during provider invocation, and after Telegram
  acceptance but before delivery persistence. It proves redelivery
  idempotency, conservative recovery on both sides of `executing`, outbox-only
  retry, concurrent provider isolation, responsive cached Controller status,
  and distinct Hub/provider polling offsets without network, credentials, or
  live services.
- Codex app-server, Hermes Gateway integration, OpenCode, and Antigravity
  adapters with isolated failure boundaries.
- Compact `/status`, `/accounts`, cached and paginated `/model`, confirmed
  single-session `/new`, `/local`, and `/return` controls.
- Private last-known-good provider catalogs with bounded callback keys and
  stale-after-failed-refresh monitoring.
- Event-driven Codex quota rotation telemetry and provider-supplied OpenCode
  reset telemetry.
- Durable masked Codex account snapshots for provider-free Controller status,
  plus private masked account hints and honest unknown-limit display for other
  providers.
- Declarative Telegram command-menu synchronization.
- Sandboxed Antigravity `accept-edits` mode; dangerous permission bypass is
  rejected.
- Optional `codex-multi-auth` with official Codex stdio fallback.
- Independent Hub, Hermes Gateway, and tlive diagnostics and monitoring.
- Privacy gate that rejects deployment identities, raw histories/session dumps,
  owner-specific paths, Telegram secrets/identifiers, and local runtime files.

## Acceptance still required per deployment

- Dedicated-user bounded Telegram baseline after deployment-local MTProto
  authorization; model/effort selection still needs owner-driven click-through.
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
