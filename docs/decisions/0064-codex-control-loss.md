# ADR 0064: Preserve control of accepted Codex turns

Status: accepted staged design; schemas48–51 source published, runtime integration pending
Date: 2026-10-08

## Context and decision

Resolved approval IDs previously consumed the pending allowance of 128 until turn
end. The resulting optional observation exception could terminate Hub's worker
path while the native turn continued. The owning contract is
[REQ-QUEUE-014](../product/DURABLE_QUEUE_AND_CONTROL.md); approval authority and
delivery uncertainty remain governed by REQ-SEC-002 and REQ-QUEUE-013.

Stage 1 restores optional observation isolation from ADR 0051. Pending requests
and typed payload-free resolved tombstones have independent bounds of 128 and 512.
Exact resolution removes a pending entry; delayed duplicates do not reopen it.
There is no tombstone eviction. Saturation or conflicting metadata retires only
observation, keeping accepted identities and mandatory stream/journal/result
handling. The worker retires eligible unattempted warnings; attempted/unknown
delivery is retained. Cleanup failure is diagnostic and receives the existing
final state-only attempt.

A fatal independent live-control observer closes only the owning primary client
to wake its native wait. Shared listeners and other turns are untouched. Socket
shutdown precedes TextIO locks; a refused shutdown is reported before locks.
The worker's protective control path opens a fresh owning WebSocket
client without fallback: exact read, one guarded interrupt per helper invocation
if active, then exact
read, with a 15-second RPC deadline budget and separate 2-second connection/
initialization deadline. Registry/SQLite guards and cleanup, including bounded
transport joins, are outside those RPC deadlines; this is not a total wall-clock
guarantee. The supervisor never selects legacy JSONL for this path. Completion
uses existing checkpoint/result recovery; otherwise uncertainty and partial
output remain. Current configuration drift cannot disable an interrupt of a
coherent immutable accepted permission snapshot. The guard is a coherent
snapshot, not a durable network fence.

The single-interrupt limit in stage 1 is per protective helper invocation; an
owner-stop path does not yet share it. Runtime events distinguish a prepared
attempt from ACK and unknown outcome, retaining exact-job provenance without
claiming terminality or durable deduplication. Notices and recovered completion
races disclose that attempt. The primary client's selected transport mode is
captured at acquisition and retained for that turn's recovery and live control;
another client's fallback cannot retarget it. While fallback is selected, the
supervisor refuses socket-control acquisition; protective interrupt and live
stop fail closed. Already selected stdio fallback retains read-only
saved-result recovery; it cannot interrupt through a non-owning process. Socket
control failure cannot select that fallback. Caller-free passive item
reads retain their separate 30-second allowance after paginated turn search;
explicit protective deadlines still bound both phases.

## Remaining control stages

Schema48 Stage 2 adds a state-owned control journal and maintenance
handler independent of productive execution. A pending stop covering an
indeterminate Codex turn retains its original provenance. Three bounded read
cycles, 30 seconds apart, cannot reset on another stop. Only a proven active exact
turn permits the one send-start-fenced interrupt; subsequent cycles read only.
Crash after that fence does not authorize resubmission. Live/protective/late
and permission-drift paths share the fence. Lease expiry does not prove the old process or RPC
stopped; local takeover remains blocked while control ownership is unconfirmed.
TurnObservation remains read-only, with terminal commit logic shared separately.
This journal/handler is not implemented by stage 1.

ExecutionJournal creates `accepted_v48` authority in the first exact acceptance
transaction when binding is coherent. Domain refusal commits the returned native
identity without a control row, then wakes recovery. Repetition cannot upgrade
it; duplicate exclusion includes checkpoints without authority rows.
Migration preserves old exact checkpoints as `legacy_read_only`;
repeated recording cannot upgrade missing or historical authority. Native
thread/turn deduplication spans roots. The journal stores a sender-token hash;
raw finish authority stays in the sender's memory. Begin validates one current
invocation lease or late-read claim, immutable permission/binding snapshot,
canonical registry root and a finite exact active observation no older than five
seconds, after acquiring the transaction lock. Remaining RPC budget is checked
again before sending.

Matched ACK or rejection plus an ended send path quiesces the sender; it does
not prove native terminality. A pre-client-call deadline refusal records
`not_sent`, quiescing only that owner while retaining its permanent fence.
Exceptions inside the client call remain unknown. A shared phase guard reserves
the send before durable begin; shutdown closes it only after bounded settlement.
Matched evidence remains in memory through a two-second state-only retry window;
one SQLite busy timeout may exceed that window. Optional runtime writes occur
after the RPC/settlement.
Unknown send, timeout, closure and claim expiry
leave the owner fenced independently of job/terminal state. Productive,
session, writer, adoption, relocation, lane and drain boundaries check that
saved canonical root. Conservative unresolved legacy aliases fail closed only
when a control owner exists. Typed scope refusal and a durable notice marker
isolate unresolved topics from unrelated sender work. ObservedTurnResults owns
exact terminal application,
including satellite/alias identity and saved raw completion when stop withholds
publication. It cannot clear sender ownership.

External workers and the embedded consumer share the accepted wait and independent
maintenance, each with its own state connection and fresh owning client without
fallback. Shutdown closes only owned clients with bounded joins; unconfirmed
shutdown cannot clear the fence. Source validation and separately authorized
exact-revision live acceptance remain required.

Physical lane cleanup has a separate pre-existing admission race: preflight and
post-Git checks cannot reserve maintenance across the filesystem operation.
Follow-up is a scoped maintenance reservation; holding SQLite locks across slow
Git operations could break another turn's durable checkpoints.

Stage 3 defines precautionary ingress-loss behavior separately. Existing passive
poll and sender signals can establish recent successful operations or observed
failures, but not exact-topic end-to-end control. Aggregate egress cannot select
a topic to stop. Missing/stale ingress needs a reviewed freshness/grace policy;
sender idle, 429 and unrelated success must not create false outage proof. No
automatic Telegram-health interruption is implemented by stage 1.

The schema49 prerequisite collects actual group polls in a separate state-domain
ledger, with startup previous-epoch CAS, a hashed instance token and increasing
sample sequence. The first successful epoch snapshot defines startup intent;
retries preserve that captured CAS/token while sampling fresh clock data. No wall
clock establishes chronological process authority. Initial read failure remains
unconfirmed until a snapshot can be captured; a captured stale CAS or a fenced
publisher cannot refresh or reacquire. Clock regression and transaction guards
defer registration or skip a sample without permanently retiring a valid owner.
Exact repeats are idempotent; sequence gaps break an unproved
failure streak. Stale publishers retire without registering again. Only the
group Controller owns registration and poll forwarding, before optional
diagnostics; direct-provider pollers do not contribute. Current-epoch success is
separate from historical confirmation, which survives restart without proving a
new process has polled. Migration imports no runtime health. HubState retains
the immediate transaction; no provider or sender dependency enters this domain.

The owning freshness/grace contract is in REQ-QUEUE-014. Its pure policy keeps
confirmation even when no episode is pending and separates recovery cutoff from
the deadline anchor, allowing an immediate successful new-epoch poll to clear
prospective uncertainty. It neither persists exact-target episodes nor selects
control. Next integration must capture trustworthy logical ingress for the exact
accepted target, retain continuity/earliest deadlines durably, and recheck
evidence and full-control consent atomically with the existing send fence.
The original Stage3 proposal included a target-bound episode for unknown exact
commentary delivery; the dated amendment below supersedes that proposal. Idle,
429 and unrelated success are not outage proof. Existing late owner-stop
claims do not yet authorize due Telegram precautions. No precautionary interrupt
or new owner-stop receipt is introduced by schema49.

Schema50 adds immutable admission-ingress and fresh-acceptance target sidecars.
HubState owns the admission/checkpoint transactions; the domain facade has no
provider or transport dependency. Logical ingress comes only from the actual
group Controller, separately from the selected provider and observer. Historical
jobs and checkpoints are never backfilled. Retry and continuation children bind
their current Reply; duplicates cannot enrich or rebind. Batch and steer compare
provenance and preserve mixed input as FIFO work; the pre-RPC transaction rechecks
it. SQL collision guards retain first-input closure and all existing control
fences, including unknown ingress, without depending on recursive DELETE triggers.
The owning contract is [section 22](../product/DURABLE_QUEUE_AND_CONTROL.md).
The private-chat regression exposed missing numeric Reply metadata. The narrow
parser correction supports exact saved-notice recovery without adding private
provider addressing or group-control semantics; unmatched private Replies keep
their ordinary-input behavior under REQ-QUEUE-004.
This prerequisite grants no precaution authority; persistent continuity/episodes,
full-control consent/send-start and live/late integration remain the next stage.

Schema51 separates the producer's retained causal watermark from the current
consecutive streak. A newly accepted failed sample preserves an established
threshold across gaps and epochs; its own cursor witnesses adoption of a
coherent schema50 threshold. Registration, duplicate/refused samples and
migration cannot capture that evidence. Restart before the first capture has
the explicit pre-upgrade continuity limit in REQ-QUEUE-014.

The exact-target reducer first compares retained success with the old cause's
logical cursor and time cutoff, then selects the complete current cause. This
avoids both equal-time false recovery and retaining an expired old deadline
after an actual, unobserved recovery followed by new failures. Registration's
zero sequence is causal evidence distinct from an absent ledger. Historical
success can retire an old cause without proving that a new epoch is healthy.
Earlier reclassification keeps its generation; genuine recovery opens a new
generation even when episode values happen to compare equal. The state-domain
facades share HubState's existing connection and transaction owner; no provider
or sender dependencies, control budgets, permanent fences or automatic sends
are added. The new additive DDL also refuses ledger and sidecar replacement and
rowid collisions without relying on recursive DELETE triggers. Stage3 control
and maintenance integration still await their own reviewed boundary.

A failure streak already past its original deadline can make a newly accepted
target immediately due; startup/stale grace does not reset that streak. Fresh
matching success may clear a due but unsent precaution as well as one before its
deadline. Future integration must recheck it inside send-start; clearing an
episode cannot erase a permanent send fence, unknown delivery or sender owner.

## 2026-10-09 amendment: one unknown progress delivery

The owner selected the third proposed policy: one unknown commentary/progress
delivery alone does not trigger precautionary interruption. It does not prove
loss of ingress, `/stop` or native approvals. The owning clause is
REQ-QUEUE-014; existing unknown-delivery storage and reconciliation remain in
force. This decision removes the need for a commentary-cause sidecar or its
consent/send-start race from the ingress-only integration. Delivery consent
does not establish ingress health and cannot suppress an independent cause.

The neutral prerequisite extracts transaction-required assessment and existing
interrupt reservation methods, while their public wrappers retain transaction
ownership. The caller alone commits or rolls back; a returned sender token is
usable only after successful commit. No native I/O occurs in that transaction.
The first covering real stop initializes a missing late-read deadline or retains
the later existing deadline, without resetting attempts, claims or provenance.
The source slice introduces no new interrupt source, claim or runtime action.

Later integration must give ingress its own explicit cause provenance, share
the permanent exact-target fence and the bounded late-read allowance, and
reassess recovery immediately before reservation. Optional assessment faults
must remain uncertainty without closing the primary stream and accidentally
entering unconditional native-loss recovery. Runtime and channel-loss
acceptance remain open and separately authorized.

## Ownership and evidence

The primary development agent owns integration. ExecutionJournal owns the
checkpoint/lease transaction; existing state owns terminal/result commits.
Workers own fresh control clients and cleanup. The helper issues no productive
RPC or approval answer. No schema, migration or sender authority is added in
stage 1. Stage 2 uses an additive control journal rather than reusing the
unrelated observation attempt budget.

Scripted RPC/worker tests cover 129+ sequential pairs, typed duplicates, saturation,
mandatory final/telemetry, retirement cleanup, active work outliving a lost
stream, exact interrupt, ACK plus active/unknown, terminal proof and completed
race recovery. A separately enabled namespace fixture uses the actual native
binary and local deterministic Responses; it is neither live inference nor
Telegram/human-approval acceptance. The supplied native executable has passed
129 sequential deny-only request/resolved pairs in one exact turn, with peak
outstanding one and retained final, and independent exact interruption after
primary connection loss. Scripted two-worker coverage combines 129 pairs per
turn with 3,600 foreign preparation notifications and both durable results.
Live two-worker/control-channel acceptance remains open. Publication requires full gates
and independent exact-source review; deployment/restart requires owner approval.
