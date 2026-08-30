# Operations map

Status: active  
Last updated: 2026-08-30

Operational truth is split by purpose:

- Repository setup, commands, service topology, and configuration examples:
  [`README.md`](../../README.md).
- Independent Hermes/tlive health and recovery:
  [`RECOVERY_PLANE.ru.md`](../RECOVERY_PLANE.ru.md).
- Threat response and fail-closed behavior:
  [`SECURITY.ru.md`](../SECURITY.ru.md) and [`SECURITY.md`](../../SECURITY.md).
- Planned post-baseline sequence: [`ROADMAP.ru.md`](../ROADMAP.ru.md).
- Complete validation gate: `python scripts/validate.py`.
- Read-only deployment diagnostics: `agents-projects-hub doctor HUB_CONFIG` and
  `agents-projects-hub monitor HUB_CONFIG`.
- Cache-only status and component health: `agents-projects-hub status HUB_CONFIG` reports
  the expected Controller, the standalone sender when configured, and every
  configured external provider worker as `healthy`, `degraded`, `stale`, or
  `unknown`. This projection reads SQLite only and never invokes a provider or
  model or optional account helper. Account/provider probes belong to their
  dedicated command and monitoring paths. Monitoring uses the same projection; general notifications still go
  only to the explicitly configured Hub Operations topic.
- External queue delivery: run one `agents-projects-hub sender HUB_CONFIG`
  alongside the selected provider workers, then set `outbox_runtime` to
  `external`. The default `controller` value preserves the previous deployment
  until that sender is ready. In external mode, the sender is the only process
  that delivers shared project-group outbox rows for all locally managed queue
  providers, including providers whose execution remains embedded.
- Optional Hub ingress: when `hub_bot` is configured, `agents-projects-hub
  controller HUB_CONFIG` polls project groups as Hub and stores its Telegram offset
  separately from Codex. Controller startup reads only that ingress token;
  provider tokens remain owned by response senders and direct-message services.
  Hub mode requires an external queue worker for every locally managed
  productive provider and the external outbox sender.
  `agents-projects-hub serve HUB_CONFIG --agent codex` remains a separate,
  direct-message-only Codex ingress with its own offset and credential boundary.
  Direct-message ingress does not overwrite the Controller health identity.
  Omitting `hub_bot` retains Codex ingress for rollback. Privacy Mode and live
  bot/menu changes remain an owner-coordinated deployment acceptance step.
- Public Telegram menu drift: `agents-projects-hub telegram-commands HUB_CONFIG`.
  Apply the exact six-command menu with
  `agents-projects-hub telegram-commands HUB_CONFIG --sync`.

## Operating rules

- Keep real configuration, state, tokens, OAuth material, sockets, logs, and
  provider sessions outside Git with restrictive permissions.
- Repair only the failed component. Do not make Hub, Hermes, tlive, or optional
  multi-auth mandatory dependencies of each other.
- Back up SQLite consistently before migration and verify recovery artifacts.
- Do not restart services, alter bot/privacy settings, or run live Telegram E2E
  as part of a documentation-only task.
- External upgrades require a backup, contract tests, smoke test, health gate,
  and rollback path.
- Exact in-flight turns are not recoverable after machine loss; communicate this
  limit instead of inferring success from stale state.
- Durable queue, sender, socket, and replacement-host procedures:
  [`QUEUE_RECOVERY.md`](QUEUE_RECOVERY.md).
- Service `SIGTERM` and `SIGINT` handlers only request shutdown. Controller and
  direct-provider ingress stop polling at the next bounded return. Workers and
  the sender check stop state around lease acquisition and return work observed
  before invocation without consuming an attempt. Cleanup waits are bounded.
  A signal can still race after the final safe-boundary check; if supervision
  terminates work past that boundary, provider recovery keeps it
  `indeterminate` unless provider-specific evidence proves a safe result.
  Telegram delivery may be duplicated after transport ambiguity, but provider
  execution is never repeated for that reason. Never manually reset ambiguous
  work to `queued` merely because shutdown occurred.

## Hermes Telegram drift and liveness

`agents-projects-hub doctor HUB_CONFIG` reports whether all registered project
chat IDs are present in both Hermes Telegram group allowlists and whether the
gateway event-loop heartbeat is fresh.

`agents-projects-hub monitor HUB_CONFIG --repair` additionally checks the
private Hermes Bot API status. A non-empty update queue is sampled twice before
repair. Missing registered groups are merged into Hermes configuration without
removing unrelated groups, and policy drift, a stale heartbeat, or a persistent
queue can trigger one Hermes-only restart per cooldown. API/network failure by
itself is alerted but does not cause a restart loop.
