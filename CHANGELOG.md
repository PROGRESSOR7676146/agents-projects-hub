# Changelog

All notable changes are documented here. The format follows Keep a Changelog,
and releases use semantic versioning while the public API is still evolving.

## [Unreleased]

### Added

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

### Changed

- Make `codex-multi-auth` an optional accelerator: prefer its shared socket when
  healthy and fall back to the official Codex stdio app-server when unavailable.
- Treat OpenCode and Antigravity as the active external provider adapters and
  require configured Telegram usernames to be valid bot usernames.
- Advance the state database to schema v6 for per-agent visible-context cursors.

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
