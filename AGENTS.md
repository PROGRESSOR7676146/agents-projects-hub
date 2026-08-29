# Project instructions

This repository is the standalone orchestration layer between Hermes Telegram topics and Codex sessions. It is not part of the Babelfish device/runtime.

## Safety invariants

- Telegram input selects only an immutable `project_id`; never accept a filesystem path from chat.
- Resolve and validate every project root against local `allowed_roots` before starting or resuming a session.
- Construct subprocess argv as arrays. Never concatenate prompts, paths or session IDs into a shell command.
- Default Codex policy is `workspace-write` plus `on-request`. Never add `danger-full-access` or automatic approval.
- Hermes is not an approval authority. Codex/tlive retains approval ownership and first-valid-answer-wins behavior.
- A Telegram topic is keyed by numeric `(chat_id, message_thread_id)`; its title is display metadata, not identity.
- Never forward hidden reasoning, credentials, environment dumps or raw terminal screen contents.
- One active Codex turn per project lane. Parallel work requires an explicit additional worktree and lane.
- Persist routing state locally with restrictive permissions; treat duplicate Telegram updates idempotently.

Use test-first development for router behavior. Live bot changes, daemon launch, service installation and credential changes require an explicit deployment task.
