# Hub General live Telegram E2E — 2026-08-29

Status: passed after one deployment-policy repair  
Project: `hub`  
Chat/thread: Hub General (`-1003935052066`, local thread `1`)  
Topic ID: `4`

## Accepted observations

| Case | Owner message | Routed agent | Result |
| --- | ---: | --- | --- |
| Ordinary main turn | `9` | Codex | Exact `CODEX_MAIN_OK`; no idle-provider turn. |
| Satellite mention | `12` | Antigravity | Exact `ANTIGRAVITY_SATELLITE_OK`; Codex remained active. |
| Real Reply to author | `15` | Antigravity | Exact `ANTIGRAVITY_REPLY_OK`; same conversation; Codex did not answer. |
| Main shared context | `18` | Codex | Saw both Antigravity turns as history and returned the exact requested context marker. |
| OpenCode mention | `21` | OpenCode | Exact `OPENCODE_SATELLITE_OK`; separate satellite session. |
| Hermes mention | recovery canary | Hermes | Exact `HERMES_RECOVERY_OK`; visible turn exported to the shared journal. |
| Post-restart turn | `32` | Codex | Exact OpenCode/Hermes context marker in the same Codex provider session. |

Routing dispatches and visible-turn records contained one target provider per
productive owner message. Locally managed OpenCode and Antigravity polling
offsets did not move; the central Codex ingress offset advanced monotonically.

## Persisted identity across controlled Hub restart

- Active agent: `codex` before and after.
- Codex provider session: `01a04ec1-ff83-7931-8dc7-a4b2c8f7f557` before and after.
- Antigravity conversation: `46ae3a3a-c05c-4039-ad80-9759b1cb97eb` before and after.
- OpenCode session: `ses_fb1386053ffe34gANyUlSNLSCV` before and after.
- Writer mode: `telegram` for all locally managed sessions.
- Codex ingress offset: `514951075` before and immediately after restart, then
  `514951076` after the successful post-restart owner message.

## Defect found and repaired

Hermes initially answered in its independent chat but not in Hub. Its Telegram
configuration allowed only Pythia. Hub and Babelfish were added to both
`platforms.telegram.allowed_chats` and
`platforms.telegram.group_allowed_chats`, preserving owner and mention policy.
After a Hermes-only restart, the queued Hub canary completed successfully.

The E2E also exposed that `systemd active` alone is insufficient transport
health evidence. Hub monitoring now checks registered-group policy, Hermes
gateway heartbeat, Bot API availability, and pending update count. Opt-in
`monitor --repair` merges missing registered groups without removing unrelated
Hermes groups and performs a cooldown-bounded Hermes-only restart for policy
drift, stale heartbeat, or a queue that remains pending across two probes.

No approval policy, owner allowlist, project-root allowlist, sandbox, or privacy
boundary was weakened during the test.
