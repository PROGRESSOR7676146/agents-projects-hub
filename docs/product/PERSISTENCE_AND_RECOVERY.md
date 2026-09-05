# Persistence and recovery requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 13. Persistence, restart, monitoring, and recovery

- **REQ-OPS-001 (Implemented):** Routing state MUST persist locally in SQLite,
  including numeric identities, provider session IDs, active agent, writer
  state, visible-context cursors, and idempotency receipts.
- **REQ-OPS-002 (Implemented):** Telegram update processing MUST be idempotent;
  duplicate updates MUST NOT create duplicate provider turns.
- **REQ-OPS-003 (Implemented):** Schema migrations MUST be versioned and create
  an SQLite-consistent pre-migration backup with rollback on failure.
- **REQ-OPS-004 (Implemented):** User services and monitoring MUST preserve
  numeric topic/session state across ordinary restarts and report component
  health without invoking a model merely to check health.
- **REQ-OPS-005 (Implemented):** Hermes Gateway and tlive MUST be monitored as
  independent, non-mandatory recovery channels. Fresh local heartbeat/status
  markers provide liveness evidence without exposing URLs or tokens.
- **REQ-OPS-006 (Implemented):** General operational alerts are bounded,
  deduplicated, and delivered
  only to one explicitly configured Hub Operations/Alerts topic. Codex is the
  primary sender; Hermes may fall back only to that same topic. Quota alerts
  include a recognizable masked account hint and never expose a full identity.
  Stale quota may remain visible as cached status but MUST NOT alert. Fresh
  Codex quota warns once per ≤5% episode and re-arms only after recovery above
  5%; unchanged conditions MUST NOT repeat. Other operational alerts are also
  edge-triggered and re-arm after recovery. The first two consecutive Telegram
  transport failures remain visible diagnostic state; the third MUST degrade
  the owning required component and emit one error edge for that episode. A
  successful transport request MUST clear it, emit one recovery edge only if
  the threshold was crossed, and re-arm the threshold. An exhausted inactive
  account stays in `/accounts` but is not an auth failure while a replacement
  is ready.
- **REQ-OPS-007 (Implemented):** Codex rotation reacts to the upstream provider
  `429` handled by the optional multi-auth proxy, never to a forecast threshold.
  The transition is always reported to Hub Operations with masked source/target
  identity. It is also reported to the work topic only when exactly one Codex
  topic is active; multiple work topics are never spammed. Observability reports
  a quota-driven pre-`429` account transition without initiating it and includes
  the replacement account's fresh status.
- **REQ-OPS-008 (Accepted):** On replacement hardware, stale writer leases from
  the lost host MUST be reset safely after verifying the old processes cannot
  exist.
- **REQ-OPS-009 (Implemented):** Controller, sender, monitor, and every configured
  provider worker MUST publish bounded SQLite health with package version, exact
  Git SHA, build time, and clean-tree assertion. Cache-only status MUST detect
  mixed or unknown required-component revisions; monitoring alerts once per
  episode and re-arms only after convergence. Health MUST contain no prompts,
  responses, exception detail, paths, command lines, environment data,
  credentials, or account identifiers.

**Recovery limit:** exact in-flight turns are not portable across process or
machine loss. Only completed state that was persisted before the loss can be
recovered. Git and Telegram history can help reconstruct work, but cannot
recreate unsaved provider context or a partially executed turn.

### Implemented queue compatibility and local provider-worker isolation

- **REQ-HUBBOT-001 (Implemented behind optional `hub_bot` configuration):** A separate Hub Telegram bot MUST become the
  central project-group identity. It MUST own group ingress, the universal
  command menu, and callbacks; it MUST have Privacy Mode disabled as a
  deployment prerequisite. Provider bots MUST NOT poll project groups, while
  retaining their own response identities and provider-specific direct-message
  endpoints. The Hub bot token MUST reside only in a restrictive private local
  file and MUST NOT appear in Git, examples, logs, or Telegram content. Hub
  ingress MUST use a durable offset identity distinct from Codex. When
  `hub_bot` is omitted, Codex remains the compatibility ingress without
  changing its existing offset. Controller startup MUST validate and read only
  the selected ingress token; provider workers, direct-message services, and
  outbox delivery retain their own credential and response-identity boundaries.
  Hub mode MUST use external queue/outbox ownership and MUST isolate every
  locally managed productive provider in its own external worker. A local
  runtime without external-worker support MUST be rejected rather than run
  inside the Controller.
- **REQ-QUEUE-001 (Implemented behind `dispatch_mode: "queue"`):** The deterministic Hub Controller MUST durably
  enqueue each admitted productive request before provider execution and MUST
  NOT wait for a provider CLI, RPC, or model turn to process local commands.
  A provider declared `managed_externally` MUST retain its native admission
  boundary and MUST NOT be accepted into the local durable queue. The Controller
  MUST ignore productive routes whose targets are all externally managed, MUST
  partition mixed multi-target routes before applying local admission rules,
  and MUST NOT invoke an externally managed provider merely to refresh a model
  catalog. Codex is the Controller's primary local provider and cannot be
  declared `managed_externally` in this architecture.
- **REQ-QUEUE-002 (Implemented for Codex, OpenCode, and Antigravity behind
  `queue_runtime: "external"`):** Provider execution MAY occur in one isolated
  worker per explicitly configured local agent ID. A worker owns its adapter or
  Codex app-server lifecycle and SQLite connection, leases only its own jobs,
  and has no Telegram transport or token-reading capability. The default
  external-worker list remains Codex for rollback compatibility; OpenCode and
  Antigravity are enabled independently through `external_worker_agent_ids`.
  Hermes remains externally managed and is not a queue-worker runtime. Failure,
  quota exhaustion, or restart of one worker MUST NOT block Controller commands
  or another provider's eligible work. This stage covers the shared project-group
  queue only; provider direct-message services remain separate legacy inline
  endpoints with their own state database.
- **REQ-QUEUE-003 (Implemented for the embedded compatibility consumer):** Productive jobs MUST execute strict FIFO within
  one numeric topic; different topics MAY execute concurrently. The target
  provider/session/model/effort snapshot MUST be immutable after enqueue.
- **REQ-QUEUE-004 (Implemented for the embedded compatibility consumer):** A durable job state machine MUST distinguish work
  not yet invoked from `executing`, result delivery, terminal failure, and
  `indeterminate` execution. An unproven in-flight turn MUST NOT be retried
  automatically. A terminal provider failure or indeterminate outcome MUST
  enqueue one bounded user-visible notice through the provider bot identity;
  delivering that notice MUST NOT convert the terminal job into a successful
  provider result.
- **REQ-QUEUE-005 (Implemented for embedded compatibility and the external sender):** Provider result persistence and Telegram delivery
  MUST use a durable outbox. Telegram delivery retry MUST NOT create another
  provider turn, and visible-context acknowledgement MUST occur only after a
  successful provider result commit. With external queue mode and the explicit
  `outbox_runtime: "external"` rollout gate, a standalone
  sender MUST fairly poll every locally managed queue agent, including embedded
  execution during mixed rollout, use each provider bot identity, recover only
  their stale delivery leases, and MUST NOT own a provider adapter
  or RPC client. The Controller MUST NOT deliver external-worker outbox rows.
- **REQ-QUEUE-006 (Implemented for the additive schema and global compatibility gate; per-provider rollout Planned):** Queue migration and per-provider rollout MUST be
  additive, feature-gated, recoverable through the existing backup discipline,
  and retain safe rollback without destroying accepted jobs. Changing an agent
  to `managed_externally` requires its accepted local jobs to be drained or
  explicitly reconciled first; Controller startup MUST fail visibly while any
  such nonterminal rows remain.
- **REQ-QUEUE-007 (Implemented):** Long-running Controller, direct-provider,
  worker, and outbox-sender processes MUST translate `SIGTERM` and `SIGINT`
  into cooperative stop requests only. They MUST stop polling and taking work
  at the next explicit safe boundary, return a lease when stop is observed
  before invocation without consuming an attempt, bound transport waits and
  joins, and restore prior process signal handlers. A signal may race after the
  final safe-boundary check; work past that boundary is treated as potentially
  started and remains subject to the existing `indeterminate`/outbox ambiguity
  rules rather than being made automatically retryable.
- **REQ-QUEUE-008 (Implemented):** While the head job of a Telegram topic is
  queued, leased, executing, or awaiting outbox delivery, the standalone sender
  SHOULD refresh Telegram's `typing` chat action through the target provider bot
  identity. Immediately after durable admission, ingress MUST also make a
  best-effort initial `typing` call so the user does not wait for the sender's
  first refresh cycle. These acknowledgements MUST NOT invoke a model or idle
  provider, and a chat-action failure cannot block execution or result delivery.
- **REQ-QUEUE-009 (Implemented):** Durable input membership MUST retain every
  Telegram `(chat_id, message_id)` exactly once even when several inputs form
  one provider turn or a later Codex input is absorbed through same-turn
  steering. A crash after an ambiguous provider acceptance MUST NOT replay that
  input automatically.

The detailed state machine, retry proof rule, reconciliation, and required
fault acceptance are normative in [ADR 0001](../decisions/0001-durable-provider-job-queue.md).
