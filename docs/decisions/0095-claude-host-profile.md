# ADR 0095: Claude host profile: CLI-equal rights without prompts

Status: accepted, 2026-11-09. Source implementation is separate from installed acceptance.

## Context

Owner decision, 2026-11-09: Claude through Hub must have the same rights as Claude
through the owner's own CLI, without approval prompts, cards or a restricted tool list.
The owner answers every request with Allow, so per-action prompts gave no real control.

## Decision

The owner explicitly selects `host` in local Claude configuration. Hub launches
the pinned native CLI, as the same UID, from the registered project root,
with the owner’s HOME and native `bypassPermissions`. No Hub permission hook,
socket, card, restricted tool list or stream tool rejection applies. Claude’s
configuration and conversations use a Hub-managed per-session directory under
`provider_home`, so user-level settings, hooks and instructions of the owner's
own CLI configuration are not inherited; project, local and managed settings load.
The configured route must be CPA; incompatible credential
selectors and settings must refuse before launch. The profile and storage are
pinned to each conversation; a change requires `/new`. A failed confined launch
must never fall back to `host`.

Registered roots, owner-only sender, one writer per root, stop/process-group
cleanup, turn journal and no replay of uncertain work remain.

## Consequences

Claude can read, change or delete owner files, credentials and Hub state, execute
commands, alter processes and contact network services without a prompt. Prompt
injection or a compromised Telegram account can cause irreversible local or
external effects. Separate conversation storage is not an access boundary: a
same-UID process can reach the owner’s other files. Backups cannot undo disclosure
or external actions. CPA routing is a launch constraint, not containment against
the running process or malicious same-UID changes.

This amends [ADR 0072](0072-trusted-personal-installation.md) and adds an
explicit alternative to, without changing, [ADR 0081](0081-claude-workstation-host-view.md).

## Amendment, 2026-11-09: turn boundary

Live acceptance found two gaps between the native CLI and Hub's turn model. A
Hub turn ends with its result, but the CLI can move long commands to background
tasks that report after that result; `host` therefore sets
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, as the confined profiles already do.
The CLI also starts each command in its own process group, so stopping the CLI
left the command running. When a `host` turn ends by stop, timeout or failure,
the worker kills and checks the turn’s own transient systemd user scope. If
cleanup cannot be verified, subsequent host turns are refused until that scope
is inactive or gone. A successful turn leaves its processes alone, as the CLI
does; systemd collects the scope when empty. Outside systemd the direct launch
has no containment for commands that leave the CLI process group.
