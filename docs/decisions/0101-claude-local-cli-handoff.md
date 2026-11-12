# ADR 0101: Claude local CLI hand-off for the host profile

Status: accepted, 2026-11-12 (owner request). Source implementation is separate from installed acceptance.

## Context

`/local` handed a Codex thread to the owner's terminal, but Claude refused it.
Under the `host` profile (ADR 0095) each native Claude session lives in its own
Hub configuration directory and reaches the model through the CPA route, whose
token must never appear in Telegram. A plain `claude --resume` in the owner's
shell would neither find the session nor use the route.

## Decision

1. An optional `route_environment` in the host `claude_file_permissions` names
   the private owner file (mode 0600) that already holds the loopback route URL
   and token for the workers. Without it, Claude transfer keeps refusing.
2. `/local` for a Claude host session sets the local writer lease and shows
   `python -I -m hermes_codex_router.claude_local --config <hub config> <hub session>`
   from the running release. That command reads local ownership, the registered
   root and the saved model/effort, verifies the private session directory with
   the same check the workers use, loads only the two route keys (the URL must
   parse to an exact loopback host and port), takes the session writer lock and
   then rechecks ownership under that lock, so a `/return` that completed in
   between refuses the hand-off. It removes other provider credentials, sets
   `CLAUDE_CONFIG_DIR` to the same session directory and replaces itself with the
   release's interactive CLI (`--resume <native id> --permission-mode
   bypassPermissions` with the session's model and effort mapped as for Hub
   turns; an effort it cannot map refuses). The lock descriptor is inherited by
   the CLI and its children.
3. Every Hub host turn takes the same lock before opening its journal launch
   record; while the local CLI runs, the turn is refused before invocation with
   `claude_session_local_busy`.
4. `/return` keeps the existing queued summary turn for non-Codex providers; it
   resumes the same native session, which already contains the local work. No
   transcript is copied.

Amendment 2026-11-12 (installed check): the interactive CLI opened its
first-run screens on a Hub session directory (theme wizard, then a folder-trust
prompt whose default exits, then an external-imports prompt). Before exec the
command now records, in that session directory only, the owner's theme,
onboarding done, trust for the registered root, the host profile's own bypass
prompt choice, and the external-imports dialog's default (imports off), keeping
all other keys. It also drops markers of a calling Claude Code session, which
otherwise turned transcript saving off. The `/return` summary is requested in
the conversation's language.

## Consequences

- The owner continues a Telegram conversation in the terminal and back, with
  the same model, route and session history.
- The lock coordinates Hub turns and this command only; a Claude process the
  owner starts by hand on that directory is not covered.
- The route token is visible to processes running as the owner, as it already
  is for the workers; this is the accepted host-profile trust model.
- Confined Claude profiles keep refusing `/local`.
