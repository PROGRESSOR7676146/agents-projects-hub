# Durable queue and control requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#durable-queue-and-control).

## 22. Durable provider queue and control

### Implemented queue compatibility and local provider-worker isolation

- **REQ-HUBBOT-001 (Implemented behind optional `hub_bot` configuration):** Configured Hub mode MUST
  own central ingress, menus and callbacks with its own durable offset; provider bots retain
  responses/direct chats and MUST NOT poll groups.
  - Privacy Mode MUST be disabled for Hub ingress; its token MUST remain private and startup MUST
    read only the selected ingress credential.
  - Omitting Hub MUST preserve Codex compatibility ingress/offset; Hub mode MUST use external
    queue/outbox and isolated workers, refusing unsupported local runtimes.
- **REQ-QUEUE-001 (Implemented behind `dispatch_mode: "queue"`):** Productive requests MUST be saved
  before execution, while Controller commands remain independent of provider waits. Externally
  managed providers MUST keep native admission, receive no local jobs/catalog calls, and be
  separated from mixed routes; Codex remains locally managed.
- **REQ-QUEUE-002 (Implemented for Codex, OpenCode, Antigravity, and a limited Claude Code CLI
  adapter behind `queue_runtime: "external"`):** Explicit external workers MUST own only their
  provider execution and database connection, never Telegram credentials/transport; one slot MUST
  run at most one turn.
  - Root capacity and Codex/Claude slots MUST default to one; extra slots require explicit external
    rollout, with independent identity/health and bounded 1–16 counts. Multiple Codex workers MUST
    NOT manage the shared server themselves.
  - Admission MUST enforce slots, canonical-root exclusion, fairness and FIFO together; reduced
    capacity MUST drain without cancellation.
  - Claude MUST use external execution and remain text-only unless separately enabled; Hermes and
    legacy direct chats remain outside this queue.
  - One worker's failure, exhaustion or restart MUST NOT block local commands or eligible work on
    independent roots.
- **REQ-QUEUE-003 (Implemented for Hub-owned queue consumers):** Topics MUST execute strict FIFO,
  with one Hub-owned productive writer per canonical root across topics/providers.
  - Independent worktrees require explicit idle-topic binding and immediate allowlisted Git-root
    validation; bind/archive MUST refuse pending, active, provider-bound, local or unresolved work.
  - Lease, local transfer and adoption MUST claim the same scope atomically; accepted
    provider/session/model/effort selections MUST remain immutable.
  - Capacity reduction MUST drain existing work; durable fair service MUST exclude stale workers
    after a bounded interval.
  - Expired unstarted leases free capacity; uncertain execution MUST block its root without
    occupying global capacity or exposing private identity through monitoring.
  - Blocked new input MUST receive a durable disposition, not silently queue; previously accepted
    work MUST keep history/FIFO, remain held and require exact owner confirmation/cancellation after
    writer return.
  - Confirmed continuations retain their documented held-tail exception; saved results MAY free
    cross-topic root capacity, but same-topic FIFO waits for final delivery.
- **REQ-QUEUE-004 (Implemented for the embedded compatibility consumer):** Users MUST be able to
  distinguish unsent, held, executing, completed, failed and uncertain work; unknown or accepted
  execution MUST NOT replay automatically.
  - Durable notices MUST explain what happened, what was saved and the next safe action; failure
    notices MUST NOT become successful results, and blocker delivery MUST remain separate from
    execution.
  - Buffered/completed output MUST be deduplicated and recoverable; failures retain bounded
    explicitly incomplete text without raw diagnostics.
  - An exact owner retry MAY repeat only a durably bound text-only request proven to have failed
    transiently before invocation, at most once.
  - Retry MUST preserve payload/context/provenance, revalidate
    root/session/generation/thread/model/effort/profile/route, respect holds/FIFO, and refuse any
    material membership or acceptance/result evidence.
  - Retry MUST NOT acquire ambient materials, batch/steer, substitute threads or reuse permissions;
    missing/conflicting proof MUST refuse or retain uncertainty.
  - Retry admission MUST save the child, provenance and control notices together; the original
    failure and its retry authorization MUST also commit together.
  - Private Reply compatibility MUST retain exact owner/destination guards;
    quotes/forwards/usernames grant no control or group provenance, and unmatched private retry
    stays ordinary input.
  - Recovery MUST use saved completion or bounded exact read-only proof without invoking/resuming
    work; mismatch, unfinished or missing evidence retains uncertainty, partials and root exclusion.
  - Owner resolution MUST be immutable, exact and idempotent, preserving original
    job/error/delivery/replay history; it MAY release only the root for new work.
  - Later exact terminal proof MAY correct a notice without erasing its history; active/unknown work
    remains blocked, and failed/interrupted continuations require explicit inspection-first new work
    in the same session with queued tails held.
  - Claude MUST persist and validate its exact native session/root before invocation; identifiers
    and provisional text alone MUST NOT prove acceptance or completion.
  - Claude completion MUST require a complete matching successful stream; saved completion/material
    notices MUST recover atomically without invocation, and missing historical material availability
    MUST stay unknown.
  - Verified provider/quota failure MUST remain distinct from uncertainty; no invented quota/reset
    values or replay, and covering stops retain precedence.
- **REQ-QUEUE-006 (Implemented for the additive schema and global compatibility gate; per-provider
  rollout Planned):** Queue rollout MUST be additive, gated, backed up and reversibly preserve
  accepted jobs. Switching to external management MUST refuse startup until local accepted work is
  drained or explicitly reconciled.
- **REQ-QUEUE-007 (Implemented):** Process shutdown MUST stop new work cooperatively at safe
  boundaries, return pre-invocation leases without attempts, and bound transport/cleanup waits. Work
  past the final boundary MUST remain potentially started, never automatically retryable.
- **REQ-QUEUE-015 (Source candidate; live acceptance pending):** An optional private drain endpoint
  MAY close admission for one exact verified worker incarnation; defaults MUST incur no added
  control I/O.
  - Its authenticated bounded protocol MUST only inspect, close admission and observe; inspection or
    drain status MUST NOT authorize maintenance or prove native terminality/durable settlement.
  - Requests MUST be exact, immutable, expiring and idempotent; acceptance survives
    expiry/disconnect, conflicts cannot replace it, and unknown responses MUST NOT retry mutations
    or retarget.
  - Admitted operations/cleanup MUST finish; the listener MUST have no
    database/provider/approval/stop capability, finite waits and private verified path custody.
  - Existing paths MUST NOT be overwritten/unlinked; cleanup may remove only its own inode, and
    obsolete protocol versions MUST refuse without fallback.
  - Socket-thread completion MUST mean physical exit of every owned transport thread after admission
    closes, not a timed join; unknown or unsupported ownership MUST remain pending/null.
  - Only the exact exclusive supported supervisor MAY prove it; managed servers, prior stdio or
    restored sockets MUST NOT manufacture eligibility.
  - Provider-client retirement belongs to the productive owner, once; unknown startup/live threads
    MUST NOT cause repeated cancellation.
  - The endpoint MUST stay absent from confined Claude and provider capabilities; trusted same-UID
    scope grants no hostile-process isolation.
  - Activation still requires exact terminality, scoped endpoint checks and restoration gates;
    source publication MUST NOT authorize deployment.
- **REQ-QUEUE-008 (Implemented):** Pending topic work SHOULD show bounded typing through the target
  provider identity without model calls; chat-action failure MUST NOT block work or results.
- **REQ-QUEUE-009 (Implemented):** Every Telegram input MUST retain unique durable membership
  through batching/steering; ambiguous acceptance MUST NOT replay it.
- **REQ-QUEUE-010 (Implemented in schema 33):** Material content, identity, binding, availability
  and consumption MUST commit with its job input and never download/invoke twice.
  - Private immutable bytes MUST be validated before copying only into the exact job's revalidated
    project root; successful consumption commits before cleanup.
  - Pre-execution retry MAY reuse the snapshot; ambiguity MUST retain it without replay, and
    session/root/provider/writer/stop/activation changes MUST NOT move it to another binding.
  - Additive migration and rollback MUST require compatible artifacts.
- **REQ-QUEUE-012 (Partially implemented in schema 37; remaining scope accepted):** Source-topic
  notices MUST distinguish queue, execution, approval wait and unknown outcome, name the observed
  blocker and offer a safe action. Queue reasons are snapshots, not continuous capacity claims;
  owner-topic links MUST use numeric identity.
  - Activity MUST reflect meaningful exact-bound provider evidence, not heartbeat/typing; bounded
    configurable quiet warnings re-arm only on progress and MUST NOT approve, stop, unlock or
    replay.
  - Active-work retries MUST join/report that work or refuse visibly; stop notices MUST separate
    pending interruption, proven terminality, held/cancelled work and unaffected topics.
  - First notice delivery MUST recheck current binding/lease/generation/writer/root and suppress
    obsolete unattempted warnings; attempted/unknown deliveries retain their evidence.
  - Approval notices MUST be payload-free and direct humans to native controls; preacceptance
    observations MUST NOT claim task acceptance, guaranteed stop or execution/control authority.
  - Observation metadata MUST remain bounded and retire safely on stale/conflicting/exhausted
    evidence without reopening requests or disrupting mandatory results, approvals, telemetry or
    cleanup. Old worker instances MUST NOT revive observation authority after restart.
  - Claude process notices MUST require actual owned-process evidence, distinguish it from native
    acceptance, and suppress misleading quiet warnings around exact permission waits.
- **REQ-QUEUE-013 (Accepted; implementation in progress):** Control delivery MUST persist its exact
  episode/destination, send-start, deadlines and positive receipts separately from execution.
  Attempted sends without proof MUST stay unknown without blind resend, failures MUST NOT change
  ownership, and database deduplication MUST NOT claim exactly-once Telegram delivery.
- **REQ-QUEUE-014 (Accepted; staged implementation under validation):** Losing mandatory control
  after exact Codex acceptance MUST wake recovery while preserving completed/partial work and
  uncertainty.
  - Protective control MUST target the exact owning server/turn with fresh proof and at most one
    guarded interrupt; acknowledgements, elapsed time, closure or exhausted observation MUST NOT
    prove terminality, release roots or authorize replay.
  - Already selected stdio MAY recover saved completion read-only; socket control failure MUST NOT
    switch to stdio or grant new invocation/approval authority.
  - Live, protective, late and permission-drift paths MUST share one permanent record that sending
    began; crash after it permits observation only, and later stops MUST NOT reset budgets.
  - Control authority MUST come only from coherent fresh acceptance and committed exact binding;
    historical/migrated/repeated/conflicting records MUST NOT acquire or expand it.
  - Current execution and late-read claims MUST be exclusive; bounded attempts/deadlines, fresh
    active proof and current root/session/writer checks MUST precede any control.
  - Proof that the sender has finished requires matched settled response or authenticated proof the
    client was never called; native terminality alone MUST NOT clear an unsettled sender.
  - Exact owner resolution plus independently matching terminal proof MAY administratively close a
    blocker after reads end, while preserving unknown history, fence, budget and no-replay rules.
  - All admission, transfer, adoption, relocation, lane/drain and server-start paths MUST use the
    same closure rules; unresolved work blocks only its own scope.
  - Covering stops MUST retain completed text privately without publication/replay; real pending
    stops take priority over precautions and steering.
  - Ingress, egress and native loss MUST remain distinct; aggregate health, typing or
    progress-delivery ambiguity alone MUST NOT authorize interruption or prove topic control.
  - Delivery consent MUST NOT hide independent control/ingress/stop/policy causes or weaken
    approvals.
  - Ingress evidence MUST bind immutably at original admission/acceptance to the exact control
    target; legacy, DM, duplicate and refused records MUST NOT invent provenance.
  - Batching/steering MUST respect ingress identity; retry/continuation children bind their own
    current Reply ingress without changing source evidence.
  - Ordered passive evidence MUST survive restart/replacement without fabricated history; stale,
    conflicting, regressing or incoherent samples MUST refuse.
  - Unrecovered causes MUST retain their original deadline/generation; only genuine later success
    can retire them, without claiming present health from historical recovery.
  - Assessment and control reservation MUST commit under their owning transaction before I/O;
    assessment alone grants no authority.
  - First send MUST freeze its exact cause; recovery/new episodes MUST NOT replenish budgets, clear
    fences or create synthetic owner stops/receipts.
  - After a send fence, remaining bounded observation MUST NOT resend or prove the sender has
    finished; terminal proof/resolution suppresses further precaution reads.
  - Optional observation faults MUST preserve mandatory results/progress/steering before any
    attempt, never impersonate native loss; after an attempt or terminal wake they MUST stop
    steering.
  - Bounded maintenance MUST reuse worker-owned control, prioritize real stops, check shutdown and
    avoid starving unrelated targets.
  - Notices MUST distinguish reservation, transmission, terminality and owner delivery, preserve
    original output/artifacts/unknown evidence, and omit optional text rather than truncate
    mandatory output.
  - Native write initiation MUST honor the original proof cutoff and response deadline even after
    queuing; unsupported transports MUST refuse and late ACKs prove no timely server receipt.
  - Once the client was called, local expiry/refusal/loss MUST preserve unknown sender ownership,
    never fabricate not-sent or terminal proof; passive monitoring MUST invoke no inference.
- **REQ-QUEUE-011 (Retired by ADR 0065):** Retired review orchestration grants no changes to queue,
  target, writer, uncertainty or replay rules; its ID remains reserved.

[ADR 0001](../decisions/0001-durable-provider-job-queue.md) remains normative for its
state machine, retry proof, reconciliation and fault acceptance.
[Section 23](DELIVERY_AND_RECEIPTS.md) owns final/progress delivery under REQ-QUEUE-005.
