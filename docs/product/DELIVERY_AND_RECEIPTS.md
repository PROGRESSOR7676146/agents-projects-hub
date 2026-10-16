# Telegram delivery and receipt requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 23. Telegram result and progress delivery

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
  part. Receipts MUST be positive non-boolean integers, never fabricated.
  Retry requires proven rejection or default-transport
  initial socket failure before any HTTP send in a call, including redirects
  and proxy CONNECT; labels/custom openers alone MUST NOT prove phase.
  Network ambiguity, malformed success
  and exhausted receipt-commit faults without durable proof MUST retain unknown delivery without changing provider
  certainty, releasing native/root uncertainty or discarding saved results,
  multipart prefix receipts and artifact references. Expired unattempted leases
  MAY requeue; expired attempted sends MUST become unknown. Cleanup after a
  committed receipt MUST NOT trigger resend. Both external and embedded senders
  share this policy. Legacy in-flight sends migrate conservatively to unknown
  without inventing a start timestamp. Legacy receipts remain historical with
  unverified provenance; only a strict post-fence receipt transaction sets the
  per-part validation version. Whole-result receipt provenance requires every
  part; historical positive IDs alone cannot establish that provenance.
  Schema54 MUST commit immutable exact-attempt identity with the send-start fence,
  binding sender, numeric destination, ordered part and payload/artifact snapshot.
  A positive exact response MUST remain recordable after lease expiry or stale
  recovery; expiry MUST NOT authorize a new send. Receipt evidence MUST commit
  before mutable delivery bookkeeping, deduplicate exact repeats and refuse
  conflicting IDs. Transient local persistence faults MAY retry at most three times
  without another transport request. Restart maintenance MUST project retained
  exact receipt evidence without transport. Changed binding, newer attempt or
  existing owner consent MUST prevent mutation of the pinned delivery snapshot;
  receipt evidence MAY remain archival. Crash before durable proof remains unknown.
  See [ADR 0069](../decisions/0069-late-receipts-and-notice-ordering.md).
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
  An unknown final or conservative legacy outbox blocks later outboxes in its numeric
  topic; a result-ready head also blocks later productive work there. New outboxes
  MUST carry immutable trusted-producer purpose: provider result or execution notice.
  Historical rows MUST remain legacy, never inferred from text or missing results.
  An unknown explicitly classified execution notice for a terminal resultless job
  MUST NOT block later saved-result delivery; native uncertainty, root/writer,
  stop, owner-hold and capacity checks remain independent. This delivery exception
  MUST NOT grant session or writer-control authority. Restart and age
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
