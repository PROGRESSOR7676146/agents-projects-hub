# Persistence and recovery requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#persistence-and-recovery).

## 13. Persistence, restart, monitoring, and recovery

- **REQ-OPS-001 (Implemented):** Numeric bindings, provider sessions, active agents, writers,
  context cursors and idempotency receipts MUST survive locally in SQLite.
- **REQ-OPS-002 (Implemented):** Duplicate Telegram updates MUST NOT create duplicate provider
  turns.
- **REQ-OPS-003 (Implemented):** Versioned migrations MUST have a consistent pre-migration backup
  and roll back on failure.
- **REQ-OPS-004 (Implemented):** Ordinary restarts MUST preserve topic/session state; monitoring
  MUST use passive, bounded, identity-free aggregates.
  - Status MUST distinguish saved results, pending delivery, unknown delivery and execution
    uncertainty, including historical owner-resolved totals and current blockers.
  - Expired execution, overdue eligible queues/deliveries and new unresolved outcomes MUST alert;
    healthy long-running work and resolved history MUST NOT create false blocking alerts.
  - Unknown nonblocking notices MUST remain separately visible; alerts MUST NOT authorize resend.
- **REQ-OPS-005 (Implemented):** Hermes and tlive MUST remain independently monitored recovery
  channels; heartbeat proves liveness, not approval-card delivery.
  - Approval health MUST require fresh exact request/session/turn evidence and a positive delivery
    reference; unconfirmed waits MUST alert once without inference, replay, permission decisions or
    automatic repair.
  - Snapshots MUST exclude inputs, prompts, paths, credentials and callback tokens; restart or
    ambiguous evidence MUST NOT establish health.
  - Attach-only tlive MUST never spawn/stop the server; migration/start MUST refuse active/unknown
    work, unsettled controls, unowned servers/sockets or mismatched artifacts without killing
    processes or deleting sockets.
  - Exact owner closure for an archived session's uncertain job and effective full-control delivery
    consent MAY remove only their own admission blockers; they MUST NOT prove execution/delivery or
    release other senders/writers.
  - Exact source/rollback identity, operator-native custody and separate live acceptance remain
    required.
- **REQ-OPS-006 (Implemented):** Bounded deduplicated operational alerts MUST use only the
  configured Hub Operations topic and Hub bot, with Hermes fallback to that same topic; missing Hub
  configuration MUST refuse.
  - Unchanged episodes MUST NOT repeat; recovery or retired conditions release them, and transport
    failures MUST degrade once at the third consecutive failure and recover once after success.
  - Monitoring MUST NOT send routine Codex context-size advice; compaction remains explicit.
- **REQ-OPS-007 (Retired by ADR 0047):** Monitoring MUST NOT read Codex rotation counters or send
  rotation notices.
- **REQ-OPS-008 (Accepted):** Lost-host writer leases MUST reset safely only after proving old
  processes cannot still exist.
- **REQ-OPS-009 (Implemented):** Every required component/worker slot MUST report bounded
  clean-build identity with exact revision; passive status MUST detect and alert on mixed/unknown
  revisions without private content, paths, commands, environment or account identities.
- **REQ-OPS-010 (Implemented):** Activation and rollback MUST verify exact clean immutable
  artifacts, configuration, consistent backup and target schema; both executables MUST support
  retained state and use the candidate's hash-locked dependencies. Manifest preparation/checking
  MUST remain read-only, without migration, services or provider calls.
- **REQ-OPS-011 (Implemented as an offline dry-run):** Release acceptance MUST rehearse candidate
  migration and distinct compatible runtime rollback entirely under temporary state, proving queued,
  outbox and uncertain work unchanged. It MUST NOT access deployment state, providers, Telegram or
  services.
- **REQ-OPS-012 (Hub application recovery planned; host recovery externally owned):** Hub MUST
  define an encrypted versioned recovery set with explicit captured scope and a verified isolated
  restore before consumers/network access. Restore MUST preserve uncertainty and spool/store
  integrity, never replay or claim uncaptured data; external host/storage/key/drill work stays
  separately owned and authorized, not a first-editor gate.
  - Hub MUST hand off the set and restore procedure, identifying off-machine storage and separately
    held keys as external prerequisites without claiming a passed host drill.
- **REQ-OPS-013 (Planned; ADR 0048):** Each monitoring cycle MUST record every evaluated alert in a
  bounded private atomic incident journal before Operations delivery, with episode identity/times
  and a runbook recommendation. It MUST contain no user content, paths, credentials or deployment
  identities and require no runtime-rollback migration.
- **REQ-OPS-014 (Planned; ADR 0048):** Enabled Hermes incident cards MUST remain model-free,
  read-only and owner-specific, with options, consequences, recommendation and explicit controls;
  only an authorized Analyze press MAY invoke a model with the incident as data.
  - Stale journals MUST alert and cold starts summarize open episodes once; cards do not replace
    Operations delivery.
  - A default-off notices-only increment MUST omit functional analysis and completion claims until
    supported; missing Gateway lifecycle support MUST refuse opt-in.
  - Exact private owner/destination, private cursor and callbacks MUST stay pinned; configuration
    changes retire controls instead of retargeting evidence.
  - Attempts MUST be saved before sending; only exact positive receipts prove delivery, ambiguity
    MUST NOT resend, and only proven rejection permits bounded retry.
  - Unsafe existing evidence MUST be retained and refused; Dismiss MUST bind the delivered card and
    owner without recovery/update/approval authority, and shutdown MUST leave no competing writer.
- **REQ-OPS-015 (Planned; ADR 0048):** Managed components MUST use immutable versions and a private
  stack manifest with passive drift detection; an independent deterministic updater MUST apply only
  an exact owner-confirmed plan.
  - Staging/check failure MUST leave links unchanged; failed health MUST restore them, with
    serialized apply/rollback and Hermes last.
  - Owner controls MUST be bounded, single-use, bound to current links/plan and invalid after
    restart; model turns MUST NOT stage/apply/rollback.
  - A Hermes-restarting switch MUST arm independent recovery of its previous version; Hub releases
    retain their own verified procedure.

Only completed persisted state is recoverable after process/machine loss; history
cannot recreate unsaved context or guarantee an in-flight turn.

### Implemented queue compatibility and local provider-worker isolation

[Section 22](DURABLE_QUEUE_AND_CONTROL.md) owns queue and control outcomes.
