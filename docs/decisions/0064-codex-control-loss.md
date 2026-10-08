# ADR 0064: Preserve control of accepted Codex turns

Status: accepted staged design; source implementation under validation
Date: 2026-10-08

## Context and decision

Resolved approval IDs previously consumed the pending allowance of 128 until turn
end. The resulting optional observation exception could terminate Hub's worker
path while the native turn continued. The owning contract is
[REQ-QUEUE-014](../product/PERSISTENCE_AND_RECOVERY.md); approval authority and
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
races disclose that attempt. Already selected stdio fallback retains read-only
saved-result recovery; it cannot interrupt through a non-owning process. Socket
control failure cannot select that fallback. Caller-free passive item
reads retain their separate 30-second allowance after paginated turn search;
explicit protective deadlines still bound both phases.

## Remaining control stages

Stage 2 adds one state-owned exact-target control journal and a maintenance
handler independent of productive execution. A pending stop covering an
indeterminate Codex turn retains its original provenance. Three bounded read
cycles, 30 seconds apart, cannot reset on another stop. Only a proven active exact
turn permits the one send-start-fenced interrupt; subsequent cycles read only.
Crash after that fence does not authorize resubmission. Live/protective/late
paths must share the fence. Lease expiry does not prove the old process or RPC
stopped; local takeover remains blocked while control ownership is unconfirmed.
TurnObservation remains read-only, with terminal commit logic shared separately.
This journal/handler is not implemented by stage 1.

Stage 3 defines precautionary ingress-loss behavior separately. Existing passive
poll and sender signals can establish recent successful operations or observed
failures, but not exact-topic end-to-end control. Aggregate egress cannot select
a topic to stop. Missing/stale ingress needs a reviewed freshness/grace policy;
sender idle, 429 and unrelated success must not create false outage proof. No
automatic Telegram-health interruption is implemented by stage 1.

## Ownership and evidence

The primary development agent owns integration. ExecutionJournal owns the
checkpoint/lease transaction; existing state owns terminal/result commits.
Workers own fresh control clients and cleanup. The helper issues no productive
RPC or approval answer. No schema, migration or sender authority is added in
stage 1. Stage 2 needs an additive control journal rather than reusing the
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
