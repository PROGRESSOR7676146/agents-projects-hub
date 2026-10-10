# Durable queue and control requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 22. Durable provider queue and control

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
  external-worker list remains Codex for rollback; `external_worker_agent_ids`
  independently enables OpenCode, Antigravity and Claude. Local Claude MUST use
  external queue mode: text-only by default, optional REQ-UX-009 image input and
  separate protected file-tool approvals.
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
  at most one child per source. Compatibility private chats MAY retain a plain
  numeric Reply solely to select an exact delivered preparation-failure or
  confirmed-failure notice under these same owner/root/session guards. An
  explicit private-topic anchor or selected quote MUST NOT select such a notice;
  forwarded input remains passive. Private Reply author usernames MUST NOT widen
  provider routing. An unmatched private `retry` Reply MUST remain ordinary
  productive input; it MUST NOT be consumed as an unsupported group control.
  No private path establishes group-ingress provenance. These
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
  bounded provisional visible text; neither UUID proves native acceptance or
  terminality. Only a validated complete
  stream with a matching session and terminal success MAY create a completion
  checkpoint. Recovery MAY deliver saved completion without invocation;
  partial, missing or conflicting evidence MUST retain uncertainty and
  root exclusion. A local failure after completion persistence MUST
  preserve recovery of that result, including when its delivery preparation
  fails again. Hub material notices MUST commit atomically with raw Claude
  completion in separate fields and recover once without invocation. Their
  bound MUST be checked before invocation. Legacy recovery with materials MUST
  warn that original availability was not saved; it MUST remain unknown, never
  reconstructed from current config or project files.
  Verified terminal provider failures and quota rejections MUST
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
  steer or grant approvals. A failed owning-socket control path MUST NOT select
  stdio fallback for recovery.
  A late pending owner stop for an indeterminate turn MUST be serviced separately
  from productive execution and read-only observation. A durable exact-target
  journal MUST share a send-start fence across live, protective and late control,
  retain covering-stop provenance, identity, attempts and bounded deadlines,
  and revalidate registry/root, session/generation/writer and retained native
  proof before control. Crash after send-start permits observation only, never
  an automatic repeat interrupt; another stop cannot reset that target's budget.
  Expired claims alone MUST NOT establish that the old control process ceased.
  Schema48 MUST create fresh control authority atomically with
  the first coherent exact accepted checkpoint. Refused control coherence MUST
  retain the returned native turn identity without granting authority; another
  job or repeated recording MUST NOT upgrade that retained target.
  Historical checkpoints MUST remain
  read-only and MUST NOT gain interrupt authority from migration or repeated
  recording. Exact native thread/turn identity MUST have one permanent
  send-start fence across live, protective, late and permission-drift paths.
  A late cycle MUST consume its persisted allowance before connecting: at most
  three cycles, separated by 30 seconds. Only the current invocation lease or
  current late-read claim, never both, MAY authorize a send. A fresh exact active
  observation MUST be no older than five seconds when the state transaction
  validates it; registry/root and retained binding MUST be rechecked.
  Only a matched ACK/rejection with an ended send path, or an owner-authenticated
  proof that the interrupt client method was never called, MAY establish sender
  quiescence. The latter MUST retain the permanent fence and MUST NOT establish
  native terminality. Shutdown MUST protect a reserved send through bounded
  settlement; transient state contention MAY retry that write, never the RPC.
  Native terminality alone MUST NOT clear an unquiesced control
  owner. Productive admission, control changes, writer transfer, adoption,
  relocation, lane maintenance and drain MUST retain that root exclusion.
  An unresolved retained scope MUST block and visibly dispose only its own
  input/queued work, without attributing an unrelated owner or aborting another
  topic's delivery cycle.
  Completed text withheld after a covering stop MUST remain saved privately
  without a result publication or automatic replay. Embedded and external
  consumers MUST share these guards and independent late maintenance, without
  weakening mandatory progress/result handling. Candidate validation and
  deployment acceptance remain separate.
  Telegram ingress and egress loss MUST be distinguished from native stream
  loss. Passive aggregate health, silence, typing, sender 429 or another topic's
  success MUST NOT establish the exact topic's controllability. Unknown delivery
  of one commentary/progress message alone MUST NOT create
  a precaution episode or authorize interruption. Its delivery evidence MUST
  remain unknown under REQ-QUEUE-005, without blind resend. Delivery-control
  consent MUST NOT establish healthy ingress or suppress an independent ingress,
  native-control, owner-stop or permission-drift cause. This policy does not
  relax approval safety or exact-terminal proof.
  Runtime precautions MUST use the exact immutable ingress target below.
  Schema49 MUST record only
  group polls (`hub`/`codex`), including empty success, never DM/health/send data.
  Startup MUST claim one captured previous-epoch CAS/token; exact repeats
  preserve epoch, stale publishers retire without reacquiring. Samples MUST fence
  identity/epoch/token/increasing sequence: repeats idempotent, stale or
  conflicting writes refused, gaps break failure streaks. Startup MUST clear
  current success, retain history; migration MUST NOT import
  health. Pure policy requires coherent aware clocks, future skew ≤5 seconds,
  heartbeat/poll/success age ≤60 and failures <3 prove only recent global polling.
  Three fresh failures anchor 30 seconds at the original third, even before
  acceptance. Missing/stale/
  startup grace: max(acceptance, retained confirmation +60) +120 seconds;
  never-confirmed: acceptance +120. Restart/reclassification MUST NOT extend
  deadlines; healthy states retain confirmation without an episode. Fresh matching
  success after the uncertainty cutoff MAY clear unsent episodes, even due; cutoff
  MUST differ from a future deadline anchor. No interrupt/receipt/approval/replay/
  root-release authority is granted. Schema50 MUST capture explicit group-ingress
  identity only in the transaction that INSERTs a new job, before its first input.
  A separate immutable sidecar MUST bind a fresh coherent schema48 acceptance
  to that ingress in the accepted-checkpoint transaction. It MUST reference the
  retained control target rather than duplicate or reselect its root/session/
  generation/native identity. Presence grants no interrupt authority. Unknown
  ingress, direct-message paths and observer/provider labels MUST NOT establish
  a binding. Migration creates empty sidecars without importing jobs or targets.
  Duplicate admission, repeat acceptance, refused coherence and historical
  checkpoints MUST NOT enrich missing provenance or replace an existing binding.
  Queue batching and same-turn steering MUST compare immutable ingress:
  equal known identities and unknown/unknown remain compatible; mixed identities
  MUST retain separate FIFO work. The transaction immediately before steering
  MUST recheck compatibility without consuming an attempt on refusal; a covering
  stop still takes precedence. Explicit preparation-retry and failed-turn
  continuation children MUST bind their current Reply ingress, independently of
  the retained source payload/context and native-permission boundary. Duplicate
  Replies MUST preserve the first child's binding. SQL identity, first-input
  closure and retained control fences MUST survive INSERT/UPDATE OR REPLACE,
  including implicit deletion when recursive triggers are disabled.
  Schema51 MUST retain successful poll cursors and unresolved failure witnesses
  atomically with newly accepted producer samples. A gap breaks the ledger's
  current streak, never an established unresolved failure. Registration and
  duplicate/refused samples MUST NOT enrich continuity. The first new failed
  sample MAY seal a coherent pre-upgrade threshold even across a gap, using its
  own witness cursor; migration MUST create empty sidecars and MUST NOT invent
  historical cursors. A restart before such capture cannot recover an erased
  pre-upgrade failure. An actual success retires older unresolved failure.
  Exact-target assessments MUST read the immutable target, accepted control,
  ledger, watermark and prior assessment in one HubState-owned transaction,
  without caller-selected evidence or native identity. Recovery MUST first use
  the old cause's fixed cursor and time cutoff. A strictly later successful
  cursor with time at or after that cutoff MAY retire it; historical recovery
  MUST NOT establish current-epoch polling health. A new failure after that
  recovery creates a new generation and deadline even at equal timestamps.
  An unrecovered episode MUST retain the complete earlier-deadline cause;
  reclassification alone MUST NOT increment its generation or extend its deadline.
  Watermark failure uses its witness as both recovery and source cursor; legacy
  current failure and missing/stale/startup use the actual read cursor, including
  a registration's `(epoch,0)`. Genuine absence is a distinct baseline. Naive,
  incoherent, future-observed or regressing evidence MUST refuse without mutation.
  Confirmation, cause bundle and assessment revision MUST commit together.
  SQL replacement and rowid collisions MUST NOT reset either sidecar or ledger.
  These assessments grant no control, delivery, approval, replay or release
  authority. Aggregate egress remains diagnostic.
  Transaction-local assessment and existing interrupt reservation MUST require
  an owning state transaction and MUST NOT commit or roll it back. Existing
  source/lease/claim rules still apply; assessment alone grants no authority.
  A reserved sender token MUST NOT authorize a native call before successful
  outer commit. Binding the first real stop MUST initialize a missing late-read
  deadline or preserve the later of its existing deadline and stop creation;
  it MUST NOT reset attempts, claims or first-stop provenance.
  Schema52 MUST capture an immutable exact ingress-cause snapshot only in the
  first send-start transaction, through a nullable parent discriminator. Old
  sends MUST NOT gain ingress provenance. A state-owned ingress reservation
  MUST reassess recovery and require a due fresh-target cause together with
  the existing exact proof, current lease or read claim and coherent binding.
  Protective/late identify invocation/read paths; only the cause sidecar proves
  ingress provenance. An ingress read claim MUST consume the same three-cycle
  allowance and 30-second spacing before connection, defer to a covering real
  pending stop, and never invent a stop row or receipt. Recovery and new episodes
  MUST NOT replenish that allowance or clear a reserved fence/sender owner.
  While the exact ingress episode remains due, claims after either an ingress
  or native send-start fence MAY consume that same allowance for bounded exact
  terminal/result observation only. They MUST NOT resend, enrich ingress
  provenance or establish sender quiescence. Recovery, exact terminal proof or
  owner resolution MUST suppress further ingress claims; a pending real stop
  retains priority, and a later stop MUST NOT replenish consumed cycles.
  Migration MUST leave the discriminator NULL and cause storage empty, preserve
  all earlier evidence and refuse replacement/rowid attacks. Native callers
  MUST recheck proof freshness and RPC deadline after commit before I/O.
  Live observation MUST prioritize real pending stops over ingress/steering.
  Pre-attempt optional assessment, connection,
  read, event and notice faults MUST preserve progress, results and steering
  without impersonating native-stream loss. Assessment MUST be limited
  to one per five seconds; ingress native observation to one per thirty seconds,
  advancing the observation deadline before acquisition across faults/recovery.
  No-send MUST keep the primary open; native attempt or terminal proof MAY wake recovery.
  Post-attempt or terminal-wake faults MUST end observation without steering.
  Quiesced authenticated `not_sent` without terminal wake MUST keep observation/
  steering across polls. The permanent fence forbids later interrupts, including
  /stop; stop priority/withholding persist. This grants no send authority.
  Maintenance MUST reuse the worker-owned thread/connection: one real-stop cycle
  before at most one ingress cycle; check shutdown between them and before each
  ingress claim. Read-only keyset pages MUST bound scans to 32 candidates and one
  frozen sweep upper key, pass healthy/refused/faulted targets and throttle
  completed sweeps for thirty seconds. Only transactional claims allocate allowance.
  Reservation-time owner stop MUST reuse proof/lease/claim without another cycle
  or synthetic stop.
  Historical notices MUST require the immutable first-send cause and independent
  control-delivery fence, after native settlement, never between reservation/RPC.
  Copy MUST separate Hub reservation, transmission, terminality and owner
  delivery without claiming owner /stop. Faults MUST NOT erase causes, change
  execution or permit resend. Explanations MUST preserve raw completion, partials,
  artifact references and unknown delivery.
  Omit optional explanations exceeding result/rendered-delivery bounds; MUST NOT
  truncate mandatory output.
  The original five-second proof window and RPC response deadline MUST separately
  bound local frame-write initiation, including time spent in the outbound queue.
  Deadline-aware control MUST preserve their earlier send-start cutoff at admission
  and immediately before the uncompressed WebSocket write, without refreshing it.
  Expired queued frames MUST be discarded and wake the waiting RPC; an unsupported
  transport MUST refuse rather than silently ignore the cutoff. An on-time write
  MAY receive its matched ACK after proof expiry within the response budget. This
  boundary MUST NOT claim timely network receipt or server processing. Once the
  interrupt client method has been called, queue expiry, refusal or transport loss
  MUST retain unknown sender ownership and its permanent fence, never establish
  authenticated `not_sent`, matched rejection, quiescence or native terminality.
  Monitoring invokes no inference; unavailable delivery is no owner receipt. See
  [ADR 0064](../decisions/0064-codex-control-loss.md).
- **REQ-QUEUE-011 (Retired by ADR 0065):** The Hub-managed review/continuation
  workflow and role generation are outside scope. Its withdrawal changes no
  existing queue, immutable target, writer, uncertainty or no-replay boundary.
  The ID remains reserved; see
  [ADR 0065](../decisions/0065-retire-hub-lead-advisor.md).

The detailed state machine, retry proof rule, reconciliation, and required
fault acceptance are normative in [ADR 0001](../decisions/0001-durable-provider-job-queue.md).
