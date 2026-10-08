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
  health without invoking a model merely to check health. The passive monitor
  result MUST expose bounded aggregate counts for accepted requests, delivered
  final results, partial outcomes, uncertain executions and recovered results,
  plus pending queue/final-delivery/progress-delivery counts and ages. These
  aggregates MUST contain no prompt, response, provider-session, project, topic
  or account identity. Final/progress unknown-delivery counts MUST remain separate
  from pending delivery/retry counts and trigger passive alerts without
  authorizing resend. Total unknown final delivery MUST remain visible after an
  owner disposition; outstanding and owner-released delivery holds MUST be
  counted separately, and only outstanding final holds keep the blocking alert
  active. The passive alert evaluator MUST report provider work with an expired execution
  lease, or due and unblocked queued work waiting over 15 minutes without an
  active worker lease. A healthy long-running execution MUST NOT alert solely
  because its original queue timestamp is old. The evaluator MUST also report
  committed Telegram final or progress delivery older than 5 minutes, and every newly
  unresolved indeterminate outcome. Historical indeterminate work with an
  operator resolution MUST remain visible in totals without keeping the alert
  active.
- **REQ-OPS-005 (Implemented):** Hermes Gateway and tlive MUST be monitored as
  independent, non-mandatory recovery channels. Fresh local heartbeat/status
  markers provide liveness evidence without exposing URLs or tokens.
- **REQ-OPS-006 (Implemented):** General operational alerts are bounded,
  deduplicated, and delivered
  only to one explicitly configured Hub Operations/Alerts topic. The configured
  Hub bot is the only Telegram sender for Hub-owned operational notifications;
  provider bot identities MUST NOT be used when `hub_bot` is absent. Hermes may
  fall back only to that same topic. A configured operations topic MUST fail
  configuration validation when `hub_bot` is absent. Routine monitoring MUST
  NOT emit Codex session context-size advice; session compaction remains an explicit user decision.
  Operational alerts are edge-triggered: unchanged conditions MUST NOT repeat,
  and an alert re-arms only after recovery. An alert episode whose condition is
  no longer evaluated, including one left by a retired feature, MUST be released
  on the next notifying cycle. The first two consecutive Telegram
  transport failures remain visible diagnostic state; the third MUST degrade
  the owning required component and emit one error edge for that episode. A
  successful transport request MUST clear it, emit one recovery edge only if
  the threshold was crossed, and re-arm the threshold.
- **REQ-OPS-007 (Retired by ADR 0047):** Hub no longer observes or reports
  Codex account rotation; the monitor MUST NOT read rotation counters or send
  rotation notices.
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
- **REQ-OPS-010 (Implemented):** Before immutable activation, a private
  deployment manifest MUST bind the exact clean-tree active and rollback wheel
  identities and SHA-256 digests, configuration digest, SQLite-consistent
  backup digest/schema, and target schema. A read-only gate MUST re-inspect
  every artifact and MUST reject activation or runtime rollback unless both
  executables support the target state schema. Runtime and E2E dependencies
  MUST come from the hash-locked export at the candidate Git revision; the
  repository gate MUST reject a stale export. Manifest creation and
  verification MUST NOT migrate state, start a service, or contact a provider.
- **REQ-OPS-011 (Implemented as an offline dry-run):** Release acceptance MUST
  include an automated rollout/runtime-rollback rehearsal that creates all
  state, configuration, release directories, manifest, backup, and activation
  pointers under one temporary root. It MUST run the candidate migration from
  schema 20 to the candidate's target schema, run the distinct compatible
  rollback artifact against the retained target state, and prove queued work, prepared outbox delivery, and an
  indeterminate job remain unchanged. It MUST NOT read deployment state, invoke
  a provider, contact Telegram, or control a service.
- **REQ-OPS-012 (Planned):** Machine-loss recovery MUST use encrypted versioned
  application snapshots and periodic cold WSL exports stored off the physical
  source machine, with recovery keys held separately. A scheduled isolated
  cold-restore drill MUST verify immutable artifacts, SQLite integrity/schema,
  project and provider-session stores, outbox spool bindings, and unchanged
  indeterminate work before any restored service or network access. Backup
  automation, WSL shutdown/export, and private-data drills require separate
  deployment authorization.
- **REQ-OPS-013 (Planned; ADR 0048):** On every cycle, before Operations
  delivery, the monitor MUST project each evaluated operational alert into a
  private, atomically replaced incident journal. An episode carries an opaque
  ID, code, severity, the bounded Operations message, and opened, last-seen and
  resolved times; the journal carries its own update time. The journal MUST
  contain no prompt, response, path, credential, account, project, topic or
  Telegram identity, MUST be bounded, and MUST NOT be state that a runtime
  rollback has to migrate. Every emitted alert code MUST have a runbook
  catalogue entry with options, their consequences and a recommended option.
- **REQ-OPS-014 (Planned; ADR 0048):** When the Hermes incident integration is
  enabled, Hermes MUST read the incident journal read-only and send the owner
  one card per new episode without invoking a model: the quoted trigger, the
  catalogue options with consequences, the recommendation, and explicit analyze
  and dismiss controls. A journal that stops updating MUST produce its own card.
  A cold start without the Hermes cursor MUST send one summary of open episodes,
  not a card per historical episode. Only an authorized owner's press of the
  analyze control MAY start a Hermes model turn, which receives the incident as
  lower-priority data, never as instructions. Enabling cards MUST stop the
  monitor's direct Hermes recovery push in the same release. Hermes-owned cards
  do not replace the Operations topic of REQ-OPS-006.
- **REQ-OPS-015 (Planned; ADR 0048):** Every component that the update tool
  manages MUST run from immutable per-version directories behind a switchable
  link recorded in a private stack manifest. `doctor` and the monitor MUST
  detect drift from that manifest passively. A deterministic update tool,
  independent of the Hub and Hermes runtimes, MUST plan read-only, then stage,
  check and switch one exact plan in dependency order with Hermes last, and
  roll back one recorded switch. A staging or check failure MUST stop before
  any link changes, and a failed health gate MUST restore the previous links.
  Apply and rollback MUST be serialized. They MUST start only from a
  deterministic owner control that is single-use, bound to the exact plan and
  the current links, expires after a bounded time, and does not survive a
  gateway restart. A model turn MUST NOT stage, apply or roll back. A switch
  that restarts Hermes MUST arm a watchdog that restores the previous Hermes
  version when the unit or its heartbeat does not recover. Project Hub releases
  keep their own procedure (REQ-OPS-010, REQ-OPS-011).

**Recovery limit:** exact in-flight turns are not portable across process or
machine loss. Only completed state that was persisted before the loss can be
recovered. Git and Telegram history can help reconstruct work, but cannot
recreate unsaved provider context or a partially executed turn.

### Implemented queue compatibility and local provider-worker isolation

The complete durable provider queue and control contract is owned by
[section 22](DURABLE_QUEUE_AND_CONTROL.md), including REQ-HUBBOT-001 and
REQ-QUEUE-001..014. Requirement IDs and complete clause text are retained.
