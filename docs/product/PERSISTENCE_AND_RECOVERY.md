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
- **REQ-QUEUE-002 (Implemented for Codex, OpenCode, Antigravity, and a limited
  Claude Code CLI adapter behind
  `queue_runtime: "external"`):** Provider execution MAY occur in isolated
  workers for explicitly configured local agent IDs. A worker owns its adapter or
  Codex app-server lifecycle and SQLite connection, leases only its own jobs,
  and has no Telegram transport or token-reading capability. The default
  external-worker list remains Codex for rollback compatibility; OpenCode and
  Antigravity and Claude are enabled independently through
  `external_worker_agent_ids`. Locally managed Claude MUST use external queue
  mode; its first repository adapter is text-only pending approval integration.
  `max_parallel_roots` MUST default to one and MUST bound simultaneous
  Hub-owned productive execution across all external workers. A value above one
  MUST require external queue mode. `codex_worker_count` MUST default to one and
  MAY configure 1–16 independently identified Codex worker processes only in
  external queue mode with `manage_codex_server: false`.
  `claude_worker_count` MUST separately default to one and MAY configure 1–16
  independent Claude CLI workers only in external queue mode; OpenCode and
  Antigravity retain one worker each. Each process MUST own its own SQLite
  connection and provider client or adapter, and one worker slot MUST NOT lease
  two active turns. The queue MUST enforce both configured provider slot counts
  and canonical-root exclusion
  transactionally, preserve provider fairness and FIFO, and drain active turns
  without cancellation when global capacity or either slot count is reduced.
  Runtime health and revision convergence MUST include every configured slot.
  Actual parallelism remains bounded by running worker processes and
  `max_parallel_roots`.
  Hermes remains externally managed and is not a queue-worker runtime. Failure,
  quota exhaustion, or restart of one worker MUST NOT block Controller commands
  or another provider's eligible work on an independent execution scope. This
  stage covers the shared project-group
  queue only; provider direct-message services remain separate legacy inline
  endpoints with their own state database.
- **REQ-QUEUE-003 (Implemented for Hub-owned queue consumers):** Productive jobs
  MUST execute strict FIFO within one numeric topic. Across topics and providers,
  at most one Hub-owned productive writer MAY own a canonical project root at a
  time. A locally created and registered Git worktree may become an independent
  execution scope only after an idle numeric topic is explicitly bound to it;
  topic identity alone does not create one. Bind/archive MUST be transactional,
  MUST refuse pending, active, provider-bound, local-writer, or unresolved work, and execution
  MUST revalidate the exact derived allowlisted Git worktree immediately before
  provider access. Lease selection,
  local-writer transfer, and saved-session adoption MUST claim or reject that
  scope in the same SQLite transaction. Different execution scopes MAY proceed
  independently up to configured capacity. Capacity reduction MUST drain
  existing execution without cancellation and MUST refuse new slots until the
  occupied count falls below the new limit. Eligible live provider workers MUST
  receive durable least-recently-granted service; a stale worker MUST stop
  blocking fairness after a bounded heartbeat window. Expired pre-execution
  leases release capacity. Expired execution becomes unresolved uncertainty,
  continues to block only its own scope, and MUST NOT consume global capacity.
  Passive health MAY expose capacity, availability, bounded worker/agent/phase
  owners, and an aggregate uncertain-scope count, but not project paths, topic
  IDs, prompts, sessions, or provider output. The target
  provider/session/model/effort snapshot MUST be immutable after enqueue.
  Persistent local/terminal ownership or unconfirmed execution on the scope
  MUST be checked in productive admission's SQLite transaction. New blocked
  input MUST have one durable disposition bound to its Telegram message and
  MUST NOT silently enter a queue that cannot progress. Already accepted
  queued work MUST keep its history and FIFO position, enter the existing
  durable hold mechanism before a writer returns, and require an exact
  owner decision to confirm or cancel; returning the lease alone MUST NOT
  start it. A confirmed job remains subject to the root writer exclusion and
  ordinary FIFO. Explicit failed-turn continuation retains its documented
  exception to an earlier held tail.
  A committed `result_ready` job MAY release its cross-topic filesystem scope
  because only durable Telegram delivery remains, while the existing same-topic
  FIFO boundary MUST continue through final-result delivery.
- **REQ-QUEUE-004 (Implemented for the embedded compatibility consumer):** A durable job state machine MUST distinguish work
  not yet invoked from `executing`, result delivery, terminal failure, and
  `indeterminate` execution. An unproven in-flight turn MUST NOT be retried
  automatically. A terminal provider failure or indeterminate outcome MUST
  enqueue a bounded user-visible notice through the provider bot identity;
  delivering that notice MUST NOT convert the terminal job into a successful
  provider result.
  Blocker and hold notices MUST be persisted separately from provider result
  delivery, sent by the authorized Hub sender, and retried as delivery only.
  Repeated Telegram updates or callbacks MUST not create another job, decision,
  or provider invocation. Owner-facing text MUST distinguish a request never
  sent to the provider, an accepted job held before execution, and executing
  work; a typing action is not evidence of execution.
  Codex queue paths MUST consume already-buffered turn events and deduplicate
  completed visible items by ID. A handled turn failure MUST retain a bounded,
  explicitly incomplete visible excerpt in its durable notice and show a safe
  cause without exposing raw diagnostics. A caught preparation failure before
  `turn/start` MUST be classified separately from uncertain invocation. An exact
  plain owner Reply `retry` to a delivered Codex preparation-failure notice MAY
  submit the saved text-only request once, only with a failure-time durable
  retry binding and a verified transient transport cause. The child MUST retain
  the original payload, context and input provenance, with the actual `retry`
  control recorded separately. Root, project, session generation, exact nullable
  thread, model, effort, permission profile and provider route MUST be rechecked
  at admission and before worker preparation. Any accepted-turn, visible-item,
  completion or result evidence MUST prohibit replay; contradictory evidence
  MUST preserve uncertainty, including when a stop is pending. Earlier holds,
  FIFO and root exclusions MUST remain effective. A retry child MUST NOT absorb
  ambient materials, batch with later input, steer into another turn, substitute
  a thread or reuse native tool permissions. Any source material membership,
  including unavailable or discarded material, MUST cause a visible refusal
  requesting the original task and materials again. Missing legacy retry binding
  MUST refuse visibly; absence of a provider turn ID alone never proves safety.
  Ticket/failure/outbox and child/provenance/control/admission notices MUST each
  commit atomically; duplicate updates and distinct repeated Replies MUST create
  at most one child per source. These
  Codex queue paths additionally persist execution identity and completed visible
  items in schema 22, separately from immutable enqueue snapshots. Thread identity
  MUST be recorded before `turn/start`; accepted turn identity MUST be recorded
  before consuming its visible items. Expired invocation leases and handled
  post-acceptance transport/commit failures MUST first enter recovery-only
  processing: a saved completed result may be committed directly,
  otherwise one bounded `thread/read` may retrieve the exact accepted completed
  turn without resuming or invoking it. Root/identity mismatch, missing acceptance,
  unavailable read capability or an unfinished turn MUST preserve uncertainty
  and any eligible saved partial text, never authorize productive replay. A
  read-only local audit MUST classify terminal indeterminate jobs from persisted
  result, completion, partial, acceptance, thread, and notification evidence;
  it MUST exclude prompt/response content from its aggregate output and MUST
  never mutate or replay provider work. An operator MAY append exactly one
  schema-23 resolution to an exact terminal indeterminate job: `acknowledged`,
  `superseded`, or `externally_completed`. Repeating the same resolution MUST be
  idempotent; replacing it or resolving another job state MUST fail. Resolution
  MUST leave the job status, error evidence, delivery state, and replay policy
  unchanged, and the audit MUST report resolved and unresolved counts. An
  unresolved `indeterminate` job MUST retain its execution scope unless the
  exact accepted turn is independently proven terminal. Its immutable operator
  resolution MAY release the scope only for new work and MUST NOT replay or
  mutate the uncertain job. Failure
  notices MUST state what happened, what Hub saved, and the next safe action.
  For an unconfirmed outcome the action MUST keep the root paused. An exact
  read-only Codex check MAY prove completed, failed, interrupted, active, or
  unknown without starting a new turn. Completed output is delivered from
  saved provider data without replay. A later proof MAY replace the initial
  unconfirmed notice with one durable corrected notice, preserving the original
  text and delivery evidence. Confirmed failed/interrupted turns keep
  their `indeterminate` job history and partial effects; separate durable
  terminal evidence releases only the execution uncertainty. Active/unknown
  remains excluded, and later reads are bounded. A confirmed failure notice
  MUST offer a reply-bound explicit inspection-first continuation as a new job
  in the same provider session; duplicate updates or choices MUST create at
  most one job. Existing queued work on that root MUST remain visible and
  paused until an owner decision. No accepted turn may be retried automatically.
  A Claude worker MUST persist its caller-chosen native session UUID and
  canonical root before invocation, validate the current generation and writer,
  and resume only that exact identity. Native message UUIDs MAY deduplicate
  bounded provisional visible text; neither session nor message identity is
  native turn-acceptance or terminality evidence. Only a validated complete
  stream with a matching session and terminal success MAY create a completion
  checkpoint. Recovery MAY deliver that saved completion without invocation;
  partial-only, missing or conflicting evidence MUST remain uncertain and
  retain root exclusion. A local failure after completion persistence MUST
  preserve recovery of that result, including when its delivery preparation
  fails again. Verified terminal provider failures and quota rejections MUST
  remain distinct from unknown outcomes, without invented quota/reset values
  or automatic replay. A covering stop retains the existing precedence.
- **REQ-QUEUE-005 (Implemented for embedded compatibility and the external sender):** Provider result persistence and Telegram delivery
  MUST use a durable outbox. Telegram delivery retry MUST NOT create another
  provider turn, and visible-context acknowledgement MUST occur only after a
  successful provider result commit. With external queue mode and the explicit
  `outbox_runtime: "external"` rollout gate, a standalone
  sender MUST fairly poll every locally managed queue agent, including embedded
  execution during mixed rollout, use each provider bot identity, recover only
  their stale delivery leases, and MUST NOT own a provider adapter
  or RPC client. The Controller MUST NOT deliver external-worker outbox rows.
  Both sender paths MUST persist a retry deadline no earlier than Telegram's
  valid `retry_after`, combined with bounded exponential backoff. Restart MUST
  retain the deadline; waiting for it MUST NOT consume delivery attempts or
  repeat provider work. In Hub mode, acknowledgement of a stop that affected an
  active or queued provider job MUST use durable control delivery under the Hub
  identity, independently of the provider result/failure outbox. Recording the
  stop and preparing its acknowledgement MUST be atomic. Duplicate stop updates
  MUST NOT create another acknowledgement. Delivery MUST NOT change execution
  certainty: an unconfirmed interruption retains its root exclusion. A stop
  that finds no work may reply directly because it records no state change.
  With external outbox ownership, a completed visible Codex commentary item MAY
  create a separate durable progress delivery under the provider identity. The
  first eligible item MAY be immediate; later items MUST be limited to at most
  one per 120 seconds for that job and bounded to Telegram-safe text. The queue
  MUST deduplicate by durable visible-item sequence, retain Telegram retry
  deadlines across sender restart, and supersede pending progress when the job
  becomes terminal. Progress delivery MUST NOT complete a job, acknowledge
  visible context, or authorize provider replay. Final-result delivery MUST have
  priority over progress delivery. Progress messages MUST request Telegram's
  silent delivery with `disable_notification`; final results and owner decisions
  MUST retain normal notification behavior. Telegram clients may still display
  a silent notification, so the flag alone does not prove the absence of screen
  or tray alerts.
  Final and progress sends MUST commit an exact current-lease send-start fence
  immediately before transport. A final fence MUST bind the first unreceipted
  part. Receipts MUST be positive integers, excluding booleans; no missing or
  malformed receipt may be replaced by a fabricated ID. Only a proven transport
  rejection permits a begun send to retry. Network ambiguity, malformed success
  and receipt-commit faults MUST retain unknown delivery without changing provider
  certainty, releasing native/root uncertainty or discarding saved results,
  multipart prefix receipts and artifact references. Expired unattempted leases
  MAY requeue; expired attempted sends MUST become unknown. Cleanup after a
  committed receipt MUST NOT trigger resend. Both external and embedded senders
  share this policy. Legacy in-flight sends migrate conservatively to unknown
  without inventing a start timestamp. Legacy receipts remain historical with
  unverified provenance; only a strict post-fence receipt transaction sets the
  per-part validation version. Whole-result receipt provenance requires every
  part; historical positive IDs alone cannot establish that provenance.
  A sending notice MUST defer late terminal reconciliation until its current
  send settles, without consuming the observation or replacing its parts.
  Later independent exact terminal proof MUST preserve a parked unknown
  notice and its parts. It MAY record native terminal evidence and bounded exact
  completion text in the existing checkpoint while keeping the indeterminate job
  and without publishing a substitute result. Such a retained notice MUST remain
  diagnostically distinct from final-result delivery. New recovered artifact
  snapshots are not retained without a durable reference; existing referenced
  artifacts remain intact. Normal replacement of a certain notice MUST archive
  all its parts, receipt provenance and artifact references atomically with the
  replacement. Historical missing parts MUST NOT be reconstructed as evidence.
  An unknown final/notice outbox blocks later outboxes in its numeric topic;
  a result-ready head also blocks later productive work there. Restart and age
  MUST NOT release these holds. A local-owner-only schema-44 control MAY record
  immutable permission to continue without confirmed delivery. Its read-only
  preview and explicit apply MUST open existing current-schema state without
  implicit migration, configuration credentials, provider calls or Telegram.
  Apply MUST require explicit agreement and the exact preview snapshot covering
  the target job, saved result if any, binding, destination and every ordered
  part including receipt provenance and artifact metadata. One HubState-owned
  immediate transaction MUST revalidate the target and snapshot and insert only
  the disposition; an exact repeat returns the same decision and a stale or
  conflicting apply fails closed. Local OS authority MUST NOT be represented
  as Telegram-authenticated owner identity.
  First apply MUST require an established canonical execution scope, refusing
  empty or legacy project scopes. The disposition MUST retain its numeric topic
  destination and project binding for its lifetime. Its execution-scope freeze
  MAY be lifted only by separate exact full-control consent covering every
  retained schema44 decision; no-op and display-only updates remain permitted.
  Apply and exact retry MUST report both
  the immutable decision and its current effect. Preview MUST explicitly disclose
  the indefinite session-control, scope-wide local/terminal transfer and agent
  drain restrictions retained for a released `result_ready` job unless an
  independently effective full-control decision removes its delivery wait. Queue continuation
  MUST NOT be presented as complete control reconciliation.
  The outbox MUST remain unknown. Parts, receipts, spool, result, checkpoint,
  job state, owner holds, stop state and writer/session authority MUST remain
  unchanged. The disposition MAY remove only that exact earlier outbox's topic
  delivery barrier and, for result_ready with its corresponding saved result,
  the productive FIFO barrier. Native uncertainty, root/writer exclusion,
  capacity, stop and held work remain independent. Session/model/agent controls,
  local transfer, connect/adoption, relocation, drain and inline-fallback guards
  MUST NOT inherit this exception. Diagnostics MUST distinguish historical
  permission from its current binding-matched effect; a changed binding cannot
  silently release another target. Retry/status copy MUST describe retained
  unknown delivery and the owner's queue decision truthfully.
  Schema-43/44 activation MUST wait for independent exact-candidate review of
  both the delivery prerequisite and this action. Source publication alone
  MUST NOT authorize activation, provider replay or automatic resend.
  A separate full-control reconciliation MAY record local-owner consent
  for one exact parked final or commentary-progress target, without confirming
  Telegram delivery. Schema46 remains the immutable storage/preview prerequisite;
  schema47 coordinates public snapshot-CAS consent with every affected control
  consumer. Partial consumer activation MUST NOT occur. No schema44 permission
  may be reinterpreted as full-control authority.
  Preview MUST accept only unknown or delivery-policy-exhausted failed targets
  without a sender lease, bind historical job/session/project/numeric destination
  and result or commentary-item identity, and hash every ordered part,
  receipt/artifact metadata and relevant retained native/owner proof. It MUST
  open existing current-schema state without migration, configuration,
  credentials, transport or inference. Preview MUST remain read-only by default;
  apply MUST require the exact preview snapshot and explicit agreement to accept
  unconfirmed delivery. One HubState-owned immediate transaction MUST revalidate
  and insert only the immutable decision. Exact repeated consent MUST return
  its original decision and current effect; another snapshot MUST NOT replace
  it. Output MUST expose no content or paths.
  A failed indeterminate final notice MUST require pre-existing exact terminal
  evidence matching the retained checkpoint or an existing exact owner resolution;
  otherwise late native recovery could replace the pinned target. Storage MUST
  retain target and selected prerequisite references without cascade, and reject
  disposition update/deletion. Historical binding matching MUST exclude today's
  active-session pointer, live scope and a later progress result; another target
  of the same job MUST require separate consent. Schema44 consent MUST remain
  unchanged and MUST NOT inherit a full-control exception. Native uncertainty,
  owner holds, stop, writers, dispatches, workflows, origin/root and capacity
  remain independent. Exact full-control final consent MAY reconcile delivery
  and saved-result FIFO clauses in session/model/agent, native transfer,
  connect/adoption, source/destination/legacy lane scope, relocation and drain
  controls; progress consent MUST NOT release a saved-result final wait. Existing
  adoption and relocation owner-resolution requirements for indeterminate work
  MUST remain unchanged. Later legitimate session/model/agent/scope changes MUST
  preserve historical consent; project, numeric destination, target/job and
  historical generation mismatches MUST make its effect fail closed. Every
  retained schema44 hold needs its own full consent before a scope move; the
  prospective destination/project guard remains mandatory. Raw unknown/failed
  evidence and aggregate counts MUST remain separate from effective blockers.
  Status, retry and outcome views MUST distinguish historical delivery from
  current consent effect; alerts and drain MUST use effective blockers.
  Schema47 activation MUST refuse a nonempty schema46 control ledger, preserving
  its rows and old schema: the preview-only prerequisite has no consent writer.
  Apply MUST NOT mutate delivery, parts,
  receipts, artifacts, native evidence, jobs, writers, stops or owner holds, nor
  authorize resend, provider replay, Reply or outcome-assessment authority.
  Source publication does not authorize live migration or activation; deployment
  requires separate exact-revision owner authorization and compatible rollback.
  The staged decision and activation boundary are recorded in
  [ADR 0063](../decisions/0063-delivery-control-reconciliation.md).
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
  joins, propagate WebSocket closure to blocked readers/senders, and restore
  prior process signal handlers. A signal may race after the
  final safe-boundary check; work past that boundary is treated as potentially
  started and remains subject to the existing `indeterminate`/outbox ambiguity
  rules rather than being made automatically retryable.
- **REQ-QUEUE-008 (Implemented):** While the head job of a Telegram topic is
  queued, leased, executing, or awaiting outbox delivery, the standalone sender
  SHOULD refresh Telegram's `typing` chat action through the target provider bot
  identity. Group ingress MAY publish an immediate best-effort `typing` action
  only when its bot is the selected provider identity; the provider sender owns
  the action otherwise. These acknowledgements MUST NOT invoke a model or idle
  provider, and a chat-action failure cannot block execution or result delivery.
- **REQ-QUEUE-009 (Implemented):** Durable input membership MUST retain every
  Telegram `(chat_id, message_id)` exactly once even when several inputs form
  one provider turn or a later Codex input is absorbed through same-turn
  steering. A crash after an ambiguous provider acceptance MUST NOT replay that
  input automatically.
- **REQ-QUEUE-010 (Implemented in schema 33):** Inbound Telegram material
  identity, binding, content class, private spool path, byte count, SHA-256,
  availability reason, and consumption state MUST be committed in the same
  SQLite transaction as its provider-job input. Duplicate
  `(chat_id, message_id, attachment_index)` receipts MUST neither download nor
  invoke twice. Raw input uses a private Hub-owned spool; provider preparation
  revalidates canonical path, regular-file status, digest, size, UTF-8 or image
  signature, then copies only to `.hub/incoming/<job_id>` below the revalidated
  execution root. Successful result commit atomically marks stored material
  consumed before best-effort raw cleanup. A stale pre-execution lease MAY retry
  the same snapshot; ambiguous execution MUST retain it without replay. Session,
  provider, root/lane, writer, stop, and activation boundaries MUST prevent a
  pending material from migrating to a different productive binding. Migration
  33 is additive; rollout and runtime rollback both require artifacts that
  declare schema-33 compatibility.
- **REQ-QUEUE-012 (Partially implemented in schema 37; remaining scope accepted):** Source-topic control
  notices MUST distinguish accepted, queued, executing, approval-waiting and
  unknown-outcome work. Queue notices MUST explain provider slots, global
  capacity, topic FIFO or canonical-root blockers and give a safe next action;
  owner-topic links MUST use numeric identity. Meaningful provider activity MUST
  be separate from heartbeat and typing. Configurable no-progress notices default
  to 300 seconds for an ordinary turn and 1,200 seconds for active tools/builds.
  Notices are edge-triggered and re-arm on meaningful progress; they never
  approve, stop, unlock or replay. A retry addressed to active work MUST join or
  report that exact work without another invocation; unsupported retry controls
  MUST fail visibly. Stop notices MUST describe numeric-topic scope, cancelled
  queued work, pending versus confirmed interruption, held work and unaffected
  work in other topics. An interrupt acknowledgement, transport loss or timeout
  MUST NOT prove terminality. The exact accepted turn must be proven terminal
  before uncertainty releases a root. These rules also apply to embedded queue
  consumers; unsupported modes MUST be refused explicitly.
  The current implemented subset is Hub-owned external queue/external outbox
  admission and execution notices, with accepted-turn activity for Codex only.
  Schema 40 extends Codex visibility to payload-free approval observations during
  `turn/start`; its implementation remains under validation.
  Queue reasons are bounded admission/handoff snapshots, not continuous capacity
  claims. `task_no_progress_seconds` and `task_tool_no_progress_seconds` configure
  the ordinary/tool thresholds; each MUST be an integer from 1 to 86,400, with
  defaults 300 and 1,200 respectively. Accepted-turn activity and first delivery of its notice MUST
  revalidate the exact live execution lease, unchanged project/scope, numeric
  topic, session agent/generation, Telegram writer and accepted native checkpoint.
  Completed checkpoints MUST NOT generate active-turn warnings. Meaningful
  activity MUST supersede never-attempted warnings for the recovered episode;
  exact approval resolution MUST supersede its never-attempted approval notice.
  Approval notices MUST remain generic and direct the owner to the native
  Codex/tlive request; no raw permission payload is stored or forwarded.
  A separate preacceptance observer MAY report a request in the prepared Codex
  session while `turn/start` is pending. It MUST NOT claim that the submitted
  task or observed turn has been accepted. Its scope MUST bind the exact live
  lease, session generation, Telegram writer, numeric destination, canonical
  root and explicit permission selection to a prepared checkpoint and the
  current persisted worker-slot epoch. Old process instances MUST NOT register
  a fresh epoch while creating observations. Missing permission context MUST
  decline this visibility. Only an exact accepted checkpoint MAY promote matching
  request metadata; early native IDs MUST NOT authorize execution, interruption,
  lease release, approval or replay. Resolved, retired, mismatched or stale
  observations MUST suppress only never-attempted notices. Promotion MUST preserve
  notice identity and immutable copy; attempted/unknown transport evidence MUST
  retain REQ-QUEUE-013 semantics independently of an epoch change. Early copy
  MUST NOT promise that `/stop` interrupts a submission before acceptance.
  Early request metadata MUST have a separate bound of 128 entries per scope.
  Payload-free event/tool/approval metadata MUST total at most 512 entries per
  job; an exhausted bound MUST NOT authorize execution, replay or approval.
  The client MUST release a pending approval entry on an exact typed resolution.
  Its separate payload-free resolved history MUST deduplicate delayed requests
  without eviction or reopening. Exhausted optional observation bounds or
  conflicting request identity MUST retire observation for that turn, preserve
  accepted identity and mandatory event/result/context/quota consumption, and
  suppress only never-attempted stale notices. Retirement cleanup faults MUST
  remain observable without aborting the mandatory stream. These client limits
  are separate from the durable per-job metadata bound.
  A separate Claude process-observation subset MAY use the ordinary threshold
  after an actual worker-owned process has been spawned. Prepared UUIDs,
  checkpoints, invocation timestamps, worker heartbeat and buffered test runners
  MUST NOT establish process-start evidence or native turn acceptance. Observation
  and first unattempted delivery MUST bind the exact current lease,
  project/scope, numeric destination, session agent/generation, Telegram writer,
  native UUID and checkpoint root, with no completion or covering stop. Completed
  visible messages advance quiet time only from the committed journal cursor;
  unknown native phase MUST NOT be relabeled commentary or tool activity.
  Existing exact-bound Claude permission requests suppress quiet notices.
  A bounded payload-free permission fingerprint MUST invalidate an unattempted
  notice even when a request begins and resolves between evaluation and send.
  Resolution, revocation or expiry MAY grant one new ordinary quiet interval;
  this is observation grace, not provider progress. Missing, changed or closed
  file-tool launches and more than 128 permission rows MUST fail observation
  closed. Retired observations MUST NOT reopen. Optional observation failures
  MUST NOT abort mandatory journal writes, provider result handling, process
  cleanup or final delivery. Attempted/unknown notices and proven-rejection
  retries retain REQ-QUEUE-013 semantics. This subset does not establish native
  acceptance, tool/build timing, progress-message parity or replay authority.
- **REQ-QUEUE-013 (Accepted; implementation in progress):** Control delivery
  MUST persist episode identity, immutable numeric destination, retry deadlines
  and positive Telegram receipts separately from provider execution. A sender
  MUST durably mark the beginning of a send before contacting Telegram. An
  expired unattempted lease MAY retry delivery; an attempted send without a
  positive persisted receipt MUST become unknown and MUST NOT be blindly resent.
  Only an explicit Telegram API rejection proves a delivery attempt retryable.
  A control-delivery failure MUST NOT change job, session or root ownership.
  Database deduplication MUST NOT be described as exactly-once Telegram delivery.
- **REQ-QUEUE-014 (Accepted; staged implementation under validation):** Loss of
  a mandatory execution or control path after exact Codex acceptance MUST wake
  the owning worker rather than leave it waiting without control. Protective
  control MUST use a fresh connection to the owning server with fallback disabled,
  bounded
  exact observation, and at most one guarded interrupt of a proven active exact
  turn. Completed output MUST remain recoverable; failed/interrupted proof MAY
  release only native uncertainty. ACK, timeout, client closure, observation
  exhaustion and unknown outcome MUST NOT prove terminality, release the root,
  create an owner-stop receipt or authorize replay. Notices MUST distinguish
  saved state from confirmed owner delivery and retain eligible partial output.
  An already selected stdio fallback MAY recover a saved completed result through
  read-only observation in a fresh process; it MUST NOT interrupt, start, resume,
  steer or answer approvals. A failed owning-socket control path MUST NOT select
  stdio fallback for recovery.
  A late pending owner stop for an indeterminate turn MUST be serviced separately
  from productive execution and read-only observation. A durable exact-target
  journal MUST share a send-start fence across live, protective and late control,
  retain covering-stop provenance, identity, attempts and bounded deadlines,
  and revalidate registry/root, session/generation/writer and retained native
  proof before control. Crash after send-start permits observation only, never
  an automatic repeat interrupt; another stop cannot reset that target's budget.
  Expired claims alone MUST NOT establish that the old control process ceased.
  Durable control fencing and late-stop service remain pending in this slice.
  Telegram ingress and egress loss MUST be distinguished from native stream
  loss. Passive aggregate health, silence, typing, sender 429 or another topic's
  success MUST NOT establish the exact topic's controllability. Any future
  precautionary interruption on prolonged unconfirmed ingress requires an
  explicit freshness/grace contract and tests for startup/restart; aggregate
  egress presently remains diagnostic, not automatic interruption authority.
  No monitor probe may invoke inference, and unavailable delivery cannot be
  reported as an owner notification. The staged boundary is recorded in
  [ADR 0064](../decisions/0064-codex-control-loss.md).
- **REQ-QUEUE-011 (Accepted; implementation pending):** An explicitly enabled
  Claude Code/Codex review workflow MUST durably bind its request, permitted
  materials, exact artifact/revision reference, advisor result, lead decision,
  and continuation to the originating project, topic, provider sessions and
  role generation. Duplicate delivery or restart MUST NOT create a second
  advisor call or lead continuation. An uncertain provider turn MUST retain
  the existing no-automatic-replay boundary. The lead MUST finish its turn
  before the advisor takes a separate queue slot; a role change MUST NOT mutate
  already accepted target snapshots.

The detailed state machine, retry proof rule, reconciliation, and required
fault acceptance are normative in [ADR 0001](../decisions/0001-durable-provider-job-queue.md).
