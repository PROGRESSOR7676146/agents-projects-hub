# ADR 0047: Retire the Codex multi-auth integration

Status: accepted; supersedes ADR 0008 and the multi-auth parts of ADR 0006 and
ADR 0017
Date: 2026-09-28

## Context

Project Hub integrated `codex-multi-auth` as an optional accelerator: an account
pool behind a loopback runtime proxy, read by the monitor, snapshotted into
state for `/status` and `/accounts`, probed by `doctor`, used for the Codex
model catalog, and reported as rotation events and pool alerts. ADR 0008 gave
the resident proxy's lifetime to a systemd drop-in, and REQ-AUTH-008 ordered
tlive after that unit.

The owner decided on 2026-09-27 to retire the integration (reliability package
B, stabilization stage 3b). The deployment already routes Codex through the
official login, so the pool code only added state, alerts and helper calls that
could disagree with the provider actually serving turns. The Codex quota label
must be fixed on the route in use, without a second account model beside it.

## Decision

Project Hub has no Codex multi-account integration. Codex runs under the
operator's official Codex login through the configured shared app-server socket
or the official stdio app-server.

| Area | Outcome |
| --- | --- |
| `codex_multi_auth_dir`, `codex_multi_auth_executable`, `codex_account_hints` | **Rejected.** Presence of any of these keys fails configuration loading, whatever the value, `null` included. The check runs right after the schema version, before any path check, filesystem access or helper call, and names only the keys, never their values. |
| Account pool (`codex_accounts.py`), rotation observer (`provider_events.py`) | **Removed.** No pool read, snapshot, rotation counter or rotation notice. |
| `/status`, `/accounts` | **Kept for other providers.** Codex shows no account; its limits come only from what the provider reports, otherwise none are shown. A pool snapshot left in state by an earlier release is ignored. A Codex agent in the generic `provider_account_hints` fails configuration loading, so the list cannot return by that route. |
| Operational alerts | **Pool, rotation, token-invalidation and runtime-proxy alerts removed.** Episodes latched by an earlier release are released on the next notifying monitor cycle by the existing alert reconciliation. |
| `doctor` | **Multi-auth checks removed.** The generic `codex_config_proxy` check of a configured loopback provider stays. |
| Codex model catalog | **Native only.** The monitor reads `model/list` from the configured socket. A catalog cached from the multi-auth matrix has a different source version and is replaced on the next refresh, even when not yet stale. |
| Supervisor and worker | **Upstream-health hook removed.** Shared-socket use, fallback to official stdio on a refused connection, and the headless stdio approval policy of ADR 0006 are unchanged. |
| Unit templates | **Removed:** `codex-multi-auth-appserver.service.d/socket-ready.conf` and `tlive.service.d/multi-auth-order.conf`. The installer had already stopped installing them. The generic `agents-projects-hub-wait-socket` tool stays. |
| Database schema | **Unchanged.** Old runtime events (under the retention of ADR 0014) and runtime counters stay as inert history; nothing reads them. |

### Migration

1. Delete the three retired keys from the Hub configuration.
2. Make sure the official Codex login works for the account that runs the Hub
   services (`codex login` or the provider's device flow).
3. Restart the Hub services. `doctor` then reports no multi-auth checks.

Configuration rollback: a configuration without the keys also loads on the
previous release, which treats them as not configured. Rolling the code back
restores the old behavior only if the keys are put back.

This is a product compatibility change, not a host uninstall. The repository
does not remove host credentials, packages, or units. Removing a leftover
helper, its data directory or its drop-ins is a separate, owner-authorized
deployment task.

## Consequences

- ADR 0008 is superseded: there is no resident multi-auth proxy to manage.
- ADR 0006 still holds for the shared socket and the stdio fallback; its mention
  of multi-auth failure as a fallback trigger no longer applies.
- ADR 0017 still holds; tlive stays approval-only for Hub turns. Its context about
  sharing a multi-auth app-server is historical.
- REQ-AUTH-003, REQ-AUTH-007, REQ-AUTH-008 and REQ-OPS-007 are retired;
  REQ-AUTH-002, REQ-AUTH-004, REQ-CMD-001, REQ-CMD-002A, REQ-CMD-003 and
  REQ-OPS-006 now describe the official-login contract.
- An operator who needs several Codex accounts switches the official login
  outside Hub. Hub does not observe or announce that switch.
