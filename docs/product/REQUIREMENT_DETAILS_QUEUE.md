# Requirement details: queue, control and delivery

Supporting reference for [durable queue and control](DURABLE_QUEUE_AND_CONTROL.md)
and [delivery and receipts](DELIVERY_AND_RECEIPTS.md), split from the
[requirement details](REQUIREMENT_DETAILS.md) on 2026-11-07 to keep each file
reviewable. The owning modules remain normative.

## DURABLE QUEUE AND CONTROL

Source: [DURABLE_QUEUE_AND_CONTROL.md](DURABLE_QUEUE_AND_CONTROL.md).

### REQ-HUBBOT-001

Optional Hub token stays restrictive/private; distinct `hub` offset versus Codex.
Startup reads chosen ingress token only, workers/DM/outbox retain separate credentials.
Without Hub old Codex offset remains. Hub requires external queue/outbox and
external-worker support for every local productive runtime.

### REQ-QUEUE-001

Queue gate: `dispatch_mode: "queue"`. All-external routes ignored; mixed routes
partition before local admission. Codex cannot be `managed_externally`.

### REQ-QUEUE-002

`queue_runtime: "external"`; `external_worker_agent_ids` defaults Codex, separately
opts OpenCode/Antigravity/Claude. Root capacity default1; Codex/Claude counts 1–16,
Codex multiples require `manage_codex_server: false`. OpenCode/Antigravity one each.
Each process owns SQLite/client/adapter, one active turn; health includes all slots.
Actual concurrency also limited by running processes. Claude external/text-only,
image opt-in separate from protected tools. Hermes external; DMs legacy/separate DB.

### REQ-QUEUE-003

SQLite scope transaction covers queue/local/adoption. Worktree path is derived,
allowlisted and Git-registered, revalidated immediately before access. Fairness is
durable least-recently-granted, stale-worker cutoff is a bounded heartbeat window.
Passive health may include capacity/availability/worker-agent-phase/uncertain count.
Enqueue pins provider/session/model/effort. Local/terminal/uncertain admission check
is transactional; accepted tails enter holds before return. Failed continuation
retains earlier-held-tail exception. `result_ready` frees cross-topic filesystem
scope but same-topic FIFO continues through final delivery.

### REQ-QUEUE-004

Not-invoked/executing/result-delivery/failure/indeterminate are distinct. Provider
failure notices use provider bot; blocker/hold notices separate Hub delivery.
Buffered events consumed, completed item IDs deduplicated, partial excerpt explicitly
incomplete/safe. Caught pre-`turn/start` preparation failure differs from ambiguity.

Retry: exact plain owner Reply `retry` to delivered Codex preparation-failure
notice, failure-time durable binding, verified transient pre-invocation cause;
one text-only child/source. Save payload/context/input provenance, control separately.
Admission/preparation recheck root/project/generation/exact nullable thread/model/
effort/profile/route. Acceptance/item/completion/result evidence blocks replay;
missing turn ID alone proves nothing. Stop cannot erase contradictions. No ambient
material/batching/steering/substitute thread/reused tool permission; any material
membership, unavailable/discarded included, refuses requesting original task/materials.
Missing legacy binding refuses. Ticket/failure/outbox atomic; child/provenance/
control/admission notice atomic. Private numeric Reply can select exact delivered
preparation/confirmed failure only; topic anchors/quotes cannot, usernames add no
route authority, forwards passive, unmatched private retry ordinary input. No
private path proves group provenance.

Schema22 stores execution separately: thread before start, accepted turn before
visible items. Expired invocation/post-acceptance transport/commit fault recovers
saved completion or one bounded exact `thread/read`, no resume/invoke. Missing
acceptance/capability/root match or unfinished turn retains uncertainty/partials.
Read-only audit classifies result/completion/partial/acceptance/thread/notification
without content/mutation. Schema23 exact terminal-indeterminate resolution once:
acknowledged/superseded/externally_completed; same idempotent, replacement/other
states refuse; preserve original status/error/delivery/replay, split totals.

Read-only exact check: completed/failed/interrupted/active/unknown. Later proof can
correct notice once while retaining original text/receipt; failed/interrupted
keeps indeterminate history/partial effects with separate terminal evidence.
Active/unknown blocks; bounded subsequent reads. Failure continuation is reply-bound
inspection-first same-session new job once; old tails held. Immutable resolution
frees scope for new work only.

Claude caller UUID/root saved pre-invoke with generation/writer check. Native
message UUID deduplicates provisional text, neither ID proves acceptance/terminal.
Complete matching successful stream alone creates completion. Saved completion
survives repeated delivery-preparation faults. Raw completion/material notices
commit atomically in separate fields; material-notice bound checked pre-invoke.
Legacy materials warn original availability unsaved, not reconstructed from current
files/config. Verified failure/quota rejection separate from unknown; covering stop
wins, no invented reset/usage.

### REQ-QUEUE-006

Additive gated rollout; nonterminal local rows block externally-managed startup.

### REQ-QUEUE-007

SIGTERM/SIGINT are cooperative; restore prior handlers, bound joins/transport,
propagate WebSocket closure to blocked readers/senders. Post-check races may have
started work; pre-invocation lease return consumes no attempt.

### REQ-QUEUE-015

Opt-in Linux drain socket under slot lock; defaults add no control I/O. Identity:
worker/kernel peer PID-UID/start ticks/boot/runtime nonce/full clean release.
Inspect discovers incarnation, not target authority. Request ID+canonical digest,
first expiry future ≤60 monotonic seconds; acceptance survives disconnect/expiry,
exact repeats same, conflicts no replacement. Listener no DB/provider/approval/stop,
not productive; frame ≤4KiB, finite waits, no blocking wait/resume/cancel/SQL.
Parent0700/socket0600, descriptor-relative custody, no overwrite/unlink existing
(stale included), cleanup only own inode. V2 rejects V1 frames/files.

Responses recheck process/request/path. `operations_drained` differs from
`socket_threads_drained`: latter null before acceptance/unsupported, false pending,
true only after admitted cleanup, client admission closed and all owned transport
threads physically exit, including failed construction/detached unknown cleanup.
Timed join no proof. Only exact exclusive worker-built supervisor, not injected/
shared/subclass/managed-server/prior-stdio; restoring socket cannot restore eligibility.
Productive owner retires eligible idle cached client once (even pre-startup drain),
listener never; unknown startup/live threads stay pending without repeated cancel.
No proof of cached-client closure/other helpers/server connection/stdio peer/native
terminal/durable settlement/history/service authority. Unknown replies no retry/
retarget; trusted same-UID only. Excluded from confined Claude/provider capabilities,
separate permission IPC. Activation retains endpoint/native/restoration gates.
[ADR 0070](../decisions/0070-worker-operations-drain-control.md),
[ADR 0071](../decisions/0071-worker-socket-thread-custody.md).

### REQ-QUEUE-008

Immediate ingress typing is allowed only if ingress is itself the target bot;
otherwise the standalone provider sender owns the action.

### REQ-QUEUE-009

Input membership key `(chat_id,message_id)` survives batch/same-turn absorption.

### REQ-QUEUE-010

Schema33 atomic material/input fields: identity/binding/class/private spool/bytes/
SHA-256/availability/consumption. Dedup key chat/message/attachment-index. Revalidate
canonical regular file/digest/size/UTF-8-or-image signature; copy only
`.hub/incoming/<job_id>` beneath checked root. Result commits consumption before
raw cleanup. Pre-start stale lease reuses snapshot; ambiguity retains. Session/
provider/root-lane/writer/stop/activation fences prevent reassignment. Both artifacts
declare schema33 compatibility.

### REQ-QUEUE-012

Implemented external queue/outbox notices; Codex accepted activity schema37,
preacceptance schema40 under validation; Claude process subset schema42.
Reasons: slots/global capacity/topic FIFO/root; bounded admission/handoff snapshots.
`task_no_progress_seconds`/`task_tool_no_progress_seconds`: integers1–86400,
defaults300/1200. Meaningful progress re-arms; heartbeat/typing does not. Embedded
shares rules, unsupported modes refuse. Activity/first send validates live lease/
project-scope/topic/agent-generation/Telegram writer/native checkpoint. Completion,
progress or exact permission resolution suppresses only unattempted stale warnings.

Preacceptance binds prepared checkpoint/explicit permissions/current persisted
slot epoch; missing permissions declines. Old process cannot register new epoch.
Only accepted matching checkpoint promotes; early IDs grant no authority or stop
guarantee. Promotion retains ID/copy; attempted/unknown survives epoch change.
Bounds:128 early requests/scope,512 payload-free event-tool-approval entries/job.
Exact typed resolution frees pending entry; separate no-eviction resolved history
deduplicates delayed requests. Exhausted optional client bound/conflict retires
observation, not mandatory event/result/context/quota; cleanup faults observable.

Claude needs actual owned spawn, not UUID/checkpoint/timestamp/heartbeat/buffered
runner. Binding includes native UUID/checkpoint root, no completion/covering stop.
Committed visible cursor advances quiet time, unknown phase stays unknown. Exact
permission suppresses; payload-free fingerprint invalidates unsent warning even
if request starts/resolves between evaluate/send. Resolution/revoke/expiry gives
one ordinary quiet interval, not progress. Missing/changed/closed tool launch or
>128 permission rows retires observation permanently. No mandatory journal/result/
cleanup/delivery disruption; acceptance/tool timing/progress parity not inferred.

### REQ-QUEUE-013

Control records: episode/immutable numeric destination/deadline/positive receipt.
Send-start before transport; unattempted expiry may retry, attempted unknown not
blindly; retry proof is REQ-QUEUE-005.

### REQ-QUEUE-014

**Control.** Fresh owning-server connection/fallback off; bounded exact read and
at most one active-turn interrupt. Already-selected stdio may read saved completion
in fresh process only. Late owner stop separate from productive/read-only paths.
Schema48 creates authority only with first coherent accepted checkpoint; refused
coherence retains native ID without upgrade via other job/re-record/migration.
One permanent native thread/turn send-start fence across live/protective/late/
permission-drift. Late allowance: ≤3 cycles, ≥30s spacing, consumed before connect.
Invocation lease XOR late-read claim; active proof age ≤5s at transaction with
root/registry/binding recheck. Expired claim proves no old-process exit.

Quiescence: matched ACK/rejection plus ended send, or owner-authenticated proof
client method never called (fence retained, not terminal proof). Shutdown protects
reservation through bounded settlement; retry DB writes, never RPC. Immutable
owner resolution + independently retained terminal proof matching checkpoint/
frozen target can retire effective blocker only after read claims end; this is
administrative closure preserving unknown/fence/budget/no replay. Archive/time/
expiry/resolution-only/terminal-only insufficient; shared all-consumer predicate.

**Schema49 polling.** Only group hub/codex polls, including empty success, never DM/
health/send. Startup captured previous-epoch CAS/token; repeats preserve epoch,
stale publisher cannot reacquire. Identity/epoch/token/increasing sequence fenced;
repeat idempotent, stale/conflict refuse, gap breaks streak. Startup clears current
success, keeps history; migration imports none. Aware coherent clocks, future skew
≤5s, heartbeat/poll/success ages ≤60s and failures<3 prove recent global polling only.
Third fresh failure anchors30s even preacceptance. Missing/stale/startup deadline
=max(acceptance,retained confirmation+60s)+120s; never-confirmed=acceptance+120s.
No restart/reclassification extension; healthy retains confirmation/no episode.
Fresh matching success after uncertainty cutoff may clear due unsent episode;
cutoff differs from future deadline anchor.

**Schema50 binding.** Explicit group ingress only on new-job INSERT before first
input. Fresh coherent accepted checkpoint atomically creates immutable sidecar
referencing control target, not duplicated/reselected identity. Unknown/DM/observer
labels/migration/duplicates/repeated/refused coherence never enrich provenance.
Batch/steer equal-known or unknown/unknown compatible; mixed FIFO. Pre-steer rechecks
without attempt on refusal, stop first. Retry/continuation child uses current Reply
ingress; repeats retain first. SQL replace/implicit delete with recursive triggers
off cannot defeat identity/first-input/control fences.

**Schema51 history.** New accepted producer sample atomically stores successful
cursor/unresolved failure witness. Gaps break current streak, not established
failure. Registration/duplicates/refusals add no continuity. First new failure can
seal coherent pre-upgrade threshold across gap with own cursor; empty migration
cannot invent history, pre-capture restart may erase it. Actual success retires
older failure. HubState transaction reads target/control/ledger/watermark/prior
assessment without caller-selected evidence. Recovery uses old cause fixed cursor/
cutoff; strictly later success at/after cutoff retires but proves no current-epoch
health. New failure creates generation/deadline even at equal timestamp. Unrecovered
reclassification retains complete earlier cause/deadline. Watermark uses witness
for source/recovery; legacy failure/missing/stale/startup uses actual cursor incl
registration(epoch,0); genuine absence distinct. Naive/incoherent/future-observed/
regressing evidence refuses unchanged. Confirmation/cause/revision atomic;
replace/rowid cannot reset ledger/sidecars; assessment grants no authority.

**Schema52 reservation.** Assessment/control reservation belong to outer transaction,
do not commit/rollback it; no I/O before successful commit. First real stop initializes
late-read deadline or keeps later existing/stop creation, never resets budget/claims/
first-stop. First send freezes ingress cause via nullable parent discriminator;
historical sends gain none. State reservation reassesses due fresh cause/recovery
with native proof/current lease-or-read/binding. Protective/late are paths, sidecar
alone proves ingress cause. Claims share three cycles/30s, defer to real stop,
never synthesize stop/receipt. Recovery/new episodes cannot replenish/clear fence/
sender. Due post-fence reads only terminal/result, no resend/enrichment/quiescence.
Recovery/terminal/resolution suppress ingress reads; later stop has priority, no
fresh allowance. Migration discriminator null/cause empty, earlier evidence and
replace/rowid guards intact. After commit recheck proof freshness/RPC deadline.

**Runtime.** Real stop precedes ingress/steer. Optional pre-attempt faults preserve
mandatory progress/results/steering, not native-loss claims. Assessment ≤once/5s,
native observation ≤once/30s, deadline advances before acquisition across faults/
recovery. No-send keeps primary open; attempted control or terminal proof can wake
recovery; post-attempt/terminal-wake faults end steering. Authenticated quiesced
not-sent without terminal wake keeps observation/steering, but permanent fence
forbids later interrupt incl stop; stop priority/withholding remains.

Maintenance uses worker-owned thread/connection, one real-stop cycle before ≤one
ingress, shutdown checks between/before. Keyset pages32/frozen upper key; pass
healthy/refused/faulted, throttle completed sweep30s. Transactional claims alone
spend allowance; reservation-time stop reuses proof/lease/claim, no extra cycle.
Historical notices need first-send cause and independent delivery fence after
native settlement, never reservation→RPC. Preserve raw completion/partials/artifacts/
unknown evidence; omit optional oversized explanation, never mandatory output.

Frame-write initiation honors original5s proof cutoff and RPC response deadline
including outbound queue. Keep earlier cutoff at admission/before uncompressed
WebSocket write, never refresh. Expired frames drop/wake RPC; unsupported transport
refuses. On-time write ACK may arrive after proof expiry within response budget,
not proof of timely network/server receipt. After client method called, expiry/
refusal/loss retains unknown sender/fence, not authenticated not-sent/rejection/
quiescence/terminality.
[ADR 0064](../decisions/0064-codex-control-loss.md).

### REQ-QUEUE-011

ADR0065 retires workflow/role generation without changing queue boundaries.

Section context: [ADR 0001](../decisions/0001-durable-provider-job-queue.md) remains
normative for state machine/retry/reconciliation/fault acceptance; section23 retains
REQ-QUEUE-005 delivery ownership.

## DELIVERY AND RECEIPTS

Source: [DELIVERY_AND_RECEIPTS.md](DELIVERY_AND_RECEIPTS.md).

### REQ-QUEUE-005

External `outbox_runtime` polls all local queue agents incl embedded mixed rollout,
recovers only their stale leases, uses provider identities; no Controller delivery
of those rows or sender adapter/RPC. Backoff: valid `retry_after` + bounded
exponential delay. Atomic Hub stop ack; idle stop direct. Commentary dedup durable
sequence: first immediate, then ≤one/120s/job; terminal supersedes pending.
`disable_notification` requests silence, not screen/tray proof.

Current-lease send-start pins first unreceipted part; receipt positive non-boolean
integer. Retry proof: rejection or default initial socket failure before any HTTP
including redirect/proxy CONNECT; custom labels insufficient. Malformed success/
exhausted local commits unknown. Legacy migration unknown with no invented start;
positive old IDs unverified. Strict post-fence version per part, all for final proof.
Schema54 attempt: sender/destination/ordered part/payload-artifact snapshot with
fence. Late exact receipt survives expiry; evidence before bookkeeping, exact dedup,
conflicting ID refusal. Local writes retry≤3 without transport; restart projects
proof. Changed binding/new attempt/consent prevents pinned mutation, evidence archival.
Notice reconciliation waits for current send without spending observation; unknown
notice keeps parts while terminal completion checkpoint is saved. New artifacts need
durable references; certain replacement archives all parts/provenance/artifacts.
[ADR 0069](../decisions/0069-late-receipts-and-notice-ordering.md).

Schema44: queue-only preview existing current-schema state, no config/credentials/
migration/network. Snapshot job/result/binding/destination/ordered parts/receipts/
artifacts; explicit agreement + HubState immediate insert-only transaction.
Canonical scope required, not empty/legacy. Lifetime numeric destination/project;
scope move needs full consent per retained hold, no-op/display allowed.
Preview discloses indefinite session/local-terminal/drain restrictions.
Trusted producer pins result/execution-notice purpose; historical purpose never
inferred. Terminal-resultless notice exception is delivery-only. Schema43/44
activation requires exact-candidate review of prerequisite/action.

Schema46 storage/preview,47 full snapshot-CAS consumers together. One unknown or
policy-exhausted final/commentary target without sender lease. Hash historical
job/session/project/destination/result-or-commentary + parts/receipt/artifact/native/
owner proof, no content/path output. Failed indeterminate notice needs existing
exact terminal checkpoint or owner resolution; prerequisite refs noncascade, decision
immutable. Matching excludes today's active pointer/live scope/later progress result.
Each target separate; final consent can reconcile all delivery guards, progress
cannot release final. Original indeterminate adoption/relocation resolutions remain.
Every44 scope hold needs full consent/destination guard.47 refuses nonempty46 ledger,
preserving schema/rows because46 had no writer. No Reply/assessment/replay/resend
authority; separate exact-revision deployment/compatible rollback.
[ADR 0063](../decisions/0063-delivery-control-reconciliation.md).

Independent Hub notice: external queue/outbox, one exact blocking-final outbox even
without successor. Bounded discovery validates sender/job/destination/result, skips
bad legacy without starvation. First send rechecks effective FIFO; receipt/consent
supersedes unattempted, attempted/unknown follows13. Preparation cannot block other
finals. Copy is saved observation directing to current status, explicit queue-only
restrictions, truthful under older compatible runtime. Other paths keep admission
explanation.
