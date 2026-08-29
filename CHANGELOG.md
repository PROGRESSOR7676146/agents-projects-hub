# Changelog

All notable changes are documented here. The format follows Keep a Changelog,
and releases use semantic versioning while the public API is still evolving.

## [Unreleased]

- Add per-agent private runtime homes for isolated Gemini credentials.
- Add a sandboxed, plan-mode Antigravity headless adapter with persistent
  conversations.
- Add a no-echo helper for installing mode-`0600` Telegram token files.
- Add observable Codex account rotation through the MIT-licensed `codex-multi-auth`
  runtime proxy while preserving provider thread IDs; retain its stdio wrapper as
  a diagnostic fallback.

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
