# Changelog

All notable changes are documented here. The format follows Keep a Changelog,
and releases use semantic versioning while the public API is still evolving.

## [Unreleased]

- Deliver Telegram Interaction Contract v2 to Codex through app-server
  `developerInstructions` on thread start and resume, keeping the stable
  contract out of user turn text and retaining bounded prompt fallback for
  providers without an accepted native instruction channel.
- Add a Codex-only scoped Telegram behavioural acceptance scenario for bounded
  short, clarification, complex-progress, and exact artifact responses without
  treating repository runner tests as live provider evidence.
- Show the acknowledged Telegram interaction-contract version for each current
  provider session in bounded local `doctor` diagnostics without expanding the
  mobile `/status` response.
- Bound `runtime_events` to 30 days and the newest 10,000 rows with atomic
  insertion/pruning, deterministic timestamp/row-ID ordering, and a compatible
  schema-21 migration that leaves health, alerts, and provider work untouched.
- Distinguish an inaccessible user-service supervisor from a confirmed inactive
  unit in local diagnostics while retaining independent Hermes heartbeat and
  tlive runtime liveness as valid recovery-channel evidence.
- Add a canonical release-metadata audit for package, changelog, project status,
  and local Git tags; contradictions fail validation while missing tags remain
  visible debt and are never created or rewritten automatically.
- Embed package version, exact clean Git SHA, and build time in wheel artifacts;
  publish release identity from every required Hub runtime and alert once when
  cached deployment revisions are mixed or unknown.
- Classify Telegram polling and delivery failures into bounded operation,
  network/API class, safe status, retry-after, consecutive-failure, and
  last-success fields. Controller and sender health now recover explicitly,
  repeated identical failures emit one edge event, and delivery retries still
  never repeat provider execution.
- Add a fixed two-provider acceptance scenario for switch isolation and explicit
  bounded `/context` retrieval.
- Make all operational warnings transition-triggered, suppress quota/auth alerts
  for an exhausted inactive account after successful replacement, and keep the
  condition visible in `/accounts` without repeated Telegram notifications.
- Detect an unreachable loopback provider configured for Codex without probing
  remote provider URLs or exposing configuration detail, and emit an
  edge-triggered alert when the supported multi-auth report explicitly marks an
  account token invalid even if another account remains ready.

## [0.6.0] - 2026-09-03

### Added

- Activate the optional separate Hub Telegram controller identity with its own
  durable ingress offset and controller-specific token loading, while retaining
  Codex as the default provider and preserving provider response/outbox and
  direct-message identity boundaries. Legacy configurations without `hub_bot`
  continue to use Codex ingress.
- Add durable provider jobs, isolated Codex/OpenCode/Antigravity workers, a
  standalone Telegram outbox sender, runtime-health snapshots, and bounded
  fault-injection coverage for crash and retry boundaries.
- Add a scoped MTProto acceptance actor with fixed checks for commands, complete
  model selection, provider connectivity, Reply provenance, passive forwards,
  multi-message bursts, and emergency-stop recovery.
- Add the shared Telegram interaction contract, native private-chat
  `Thinking…` drafts, group typing refresh, bounded burst collection,
  same-turn Codex steering, and deterministic emergency stop.
- Add optional Antigravity status-line telemetry and provider-neutral cached
  account/limit presentation.
- Add HTML-aware multipart Telegram replies with per-part durable delivery and
  retry from the first part that has no recorded Telegram message ID.

### Changed

- Deliver the full current Telegram interaction contract once to existing
  provider sessions after rollout, acknowledge it only after successful work,
  and use the compact reminder on later turns.
- Keep provider availability separate from cached quota: known runtime or
  network failures now remain visibly unavailable even when cached quota exists.
- Deliver provider failures through the durable outbox and compact all provider
  reply/status metadata for mobile Telegram use.
- Keep unaddressed burst continuations on the provider selected by the first
  part, including satellite providers that are not active for the topic.

### Fixed

- Stop discarding valid staged artifacts merely because they appear after a
  fixed attachment-count cutoff; retain per-file and aggregate byte bounds.
- Deliver validated artifacts from Hub-owned direct-message and legacy inline
  turns while keeping Hermes's native Gateway transport independent.
- Attribute Hub-owned inline callbacks to the Hub ingress identity rather than
  the default provider identity, keeping callback audit and diagnostics exact.
- Prevent stale Unix socket inodes after abrupt host or WSL shutdown from
  releasing tlive startup ordering before the rotating Codex app-server accepts
  real connections.
- Fail scoped Telegram acceptance checks promptly when unrelated traffic enters
  the dedicated canary topic, and stop after the first failed scenario instead
  of accumulating later work behind an unhealthy provider.
- Prevent a provider/RPC failure from terminating central Telegram ingress or
  cascading into unrelated providers.
- Distinguish a forum topic's protocol reply anchor from a user-selected Reply,
  preserving correct routing and burst collection.
- Detect Antigravity unsupported-network failures from private per-turn logs and
  terminate OpenCode quota-exhausted turns without leaving hung CLI processes.
- Recover sender health after idle cycles and fall back from an unhealthy
  optional Codex multi-auth upstream to the official stdio app-server.
- Prevent the isolated Codex stdio fallback from deadlocking on an approval
  request that no tlive companion connection can receive; escalation now fails
  closed while `workspace-write` remains enforced.

## [0.5.1] - 2026-08-30

### Added

- Add a mandatory privacy gate for both the proposed tree and every reachable
  Git blob. CI fetches full history and rejects private deployment fingerprints,
  real identities and paths, Telegram secrets/IDs, session dumps, internal
  handoffs, and secret-bearing runtime files.

### Changed

- Remove deployment-specific projects, accounts, bot identities, local paths,
  live transcripts, session exports, and obsolete internal planning documents
  from the publishable repository.

## [0.5.0] - 2026-08-30

### Added

- Add central-ingress Telegram E2E coverage, Hermes
  registered-group policy/heartbeat/Bot API/queue monitoring, and provide
  cooldown-bounded Hermes-only self-repair for policy drift or stuck ingress.
- Establish a canonical product-requirements baseline and proportionate durable
  documentation map for current status, decisions, risks, operations, testing,
  and future-agent orientation.
- Add independent Hermes Gateway and tlive recovery-plane health checks,
  dual-channel operational alert delivery, an optional tlive user-service
  template, and a Russian recovery runbook.
- Record only the numeric chat ID and bounded title of authorized but unbound
  Telegram project groups so an existing group can be bound without storing its
  message text.
- Add per-agent systemd health probes plus bounded startup and Telegram-error
  events for OpenCode and Antigravity pollers.
- Monitor each locally managed bot's access to every configured project group.
- Route real Telegram replies exclusively to the bot that authored the replied
  message; manually selected Telegram quotes and pasted textual quotes continue
  to follow the topic's active agent.
- Add a single central group ingress for locally managed provider identities and
  a bounded visible-topic journal whose unseen delta is supplied to other agents
  on their next productive turn without triggering observer model calls.
- Add compact `/status` and `/accounts` summaries plus a cached, paginated
  `/model` menu with collision-checked short callback identifiers.
- Cache each provider's last-known-good model catalog in private local state and
  alert Operations only when a failed refresh leaves that catalog stale.
- Add event-driven Codex quota rotation notices, exact OpenCode reset telemetry,
  declarative Telegram command-menu synchronization, and `/local`/`/return`
  session publishing.
- Require an owner confirmation bound to the current session before `/new`
  archives it; remove the mass-reset `/new all` operation.

### Changed

- Make `codex-multi-auth` an optional accelerator: prefer its shared socket when
  healthy and fall back to the official Codex stdio app-server when unavailable.
- Treat OpenCode and Antigravity as the active external provider adapters and
  require configured Telegram usernames to be valid bot usernames.
- Run Antigravity in sandboxed `accept-edits` mode instead of forcing plan mode.
- Advance the state database to schema v9 for visible-context cursors, compact
  status telemetry, local-writer state, and runtime checkpoints.

## [0.4.0] - 2026-08-29

### Added

- Add per-agent private runtime homes for isolated Gemini credentials.
- Add a sandboxed, plan-mode Antigravity headless adapter with persistent
  conversations.
- Add a no-echo helper for installing mode-`0600` Telegram token files.
- Add observable Codex account rotation through the MIT-licensed `codex-multi-auth`
  runtime proxy while preserving provider thread IDs; retain its stdio wrapper as
  a diagnostic fallback.
- Add cooldown-deduplicated deployment, account/quota, and stuck-dispatch alerts
  with a five-minute systemd timer template.
- Add explicitly confirmed worktree-lane topic binding and branch-retaining safe
  cleanup with persistent cleanup audit state.
- Add contract coverage for the Hermes public turn-export hook.

### Changed

- Document local Codex model-catalog startup configuration for custom account
  proxies whose `/models` response is incompatible.
- Advance the state database to schema v5 for alert delivery cooldowns and lane
  cleanup audit timestamps.

## [0.3.0] - 2026-08-29

### Added

- Persistent Telegram-topic to Codex-thread routing.
- Bidirectional Codex and Hermes context handoffs.
- Model and agent switching with bounded visible context.
- Explicit terminal writer takeover and release.
- Fail-closed project, topic, owner, sandbox, and approval validation.
- Automated CI, CodeQL, dependency updates, and security documentation.
- Reproducible service installation and structured diagnostics.
- Versioned state migrations and SQLite-consistent backups.
- WSL, Linux, macOS, and tmux-only terminal backend configuration.
- Gemini/OpenCode CLI adapters and worktree-lane foundations.
- Local project administration, dispatch health state, and `/status`.
