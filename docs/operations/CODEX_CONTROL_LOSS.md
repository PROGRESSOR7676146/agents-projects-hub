# Codex control-loss acceptance and recovery

Status: staged source fix; full control-loss acceptance pending
Owner: lead development agent. Last verified Stage 1 revision: `fe7bb51ffa8dc491a33c624614dcff57ab40208d`.
Schema48 Stage 2 candidate remains under validation; no deployment acceptance.

Follow [REQ-QUEUE-014](../product/PERSISTENCE_AND_RECOVERY.md) and
[ADR 0064](../decisions/0064-codex-control-loss.md). A worker marked idle or a
delivered failure notice does not establish that the exact native turn stopped.

## Investigation without replay

Use the read-only triage commands in [queue recovery](QUEUE_RECOVERY.md).
Preserve a consistent backup and privately record the exact accepted checkpoint,
installed clean revision and all required component revisions. Distinguish:

| Evidence | Safe conclusion |
| --- | --- |
| Observation bound exhausted | Optional visibility may retire; mandatory consumption must continue |
| Lost native stream/control observer | Exact-turn recovery is required; client closure is not termination |
| `codex_protective_interrupt_attempted` event | Prepared exact-job attempt; native receipt is unproved |
| `codex_protective_interrupt_acknowledged` event | RPC response received; native terminality is still unproved |
| `codex_protective_interrupt_unconfirmed` event | ACK unproved; inspect the durable outcome and exact target |
| Interrupt ACK | Request acknowledged; independently read exact terminal state |
| Durable send-start with unknown response | Sender remains fenced even after native terminal proof |
| Matched ACK/rejection and ended send path | Sender quiesced; native terminality remains independent |
| Authenticated `not_sent` before calling the client | Sender quiesced; permanent fence and native uncertainty remain |
| Active/unknown exact turn | Root remains held; no retry, reset or replacement writer |
| Prepared notice | Saved for delivery; owner receipt is not established |
| Aggregate Telegram transport failures | Diagnose ingress/egress separately; exact-topic loss is unproved |

Do not resolve uncertainty merely to unblock a root, restart an active worker,
stop the shared server or reset queue rows. Use a separately authorized exact
native stop or independent recovery channel when the late-stop handler is not
available. Recheck terminal state before any new same-root work. Restart alone
cannot close the buffer/control incident.

These runtime events retain exact-job provenance in private state but are
retention-limited diagnostics. Schema48's exact-target journal owns the durable
send fence. Failure notices and recovered completion races identify an actual
protective attempt; optional event-write failure cannot cancel mandatory fenced
control or result reading. An already selected stdio fallback permits only
saved-result reads from a fresh process, without native interrupt or replay.

For schema48, late pending stop is independent of productive work: at most three
read cycles with 30-second spacing and a claim consumed before connection.
Live/protective/late/permission-drift share one interrupt fence. Crash after
send-start permits observation only; a second stop or restart cannot reset the
budget. Historical accepted targets migrate read-only. Unknown sender ownership
may retain a root indefinitely: terminal proof alone is not sender quiescence.
Do not clear it by editing the database or releasing a writer. Activation requires
a schema48-compatible runtime rollback; an older runtime is unsafe against
retained migrated state.

Shutdown waits for a reserved fenced send and its bounded settlement instead of
destroying a matched response. Brief SQLite contention retries only settlement,
with no second RPC. A control-coherence refusal retains exact accepted identity
without new authority; neither repeat recording nor another job can upgrade it.
Ordinary `/stop` cannot create missing authority; use a separately authorized
exact native stop or an independent recovery channel. A socket stop that sends
no interrupt leaves the primary consuming progress and the private saved final;
the pending stop still withholds publication. Idle late-control polling takes
no write lock until a due claim or an unbound covering stop is observed.
An unresolved topic receives its own truthful refusal/hold, without blocking
unrelated deliveries or asserting a relationship to another sender.

## Required acceptance matrix

Automated source tests and the opt-in native fixture must precede any live run.
Native fixtures use disposable namespace endpoints and deterministic local
Responses, with no real login, remote model or installed service access.

1. More than 128 sequential exact request/resolved pairs in one native turn;
   pending capacity is reclaimed, delayed duplicates and integer/string IDs
   cannot reopen approval, final/context/quota survive. Test bounds and cleanup
   failure separately; the synthetic deny companion never grants authority.
2. Two independent workers/roots preserve both results while the second prepares
   through more than 1,024 foreign notifications. Approvals, final events and
   visible telemetry survive; no additional start/resume or unrelated restart.
3. Primary connection loss while the exact native turn remains active: a fresh
   owning client targets only that turn. ACK plus active/unknown retains root;
   exact interrupted/failed/completed evidence is handled without replay.
4. Late owner stop after indeterminate and exhausted passive observations:
   restart around claim/send-start/ACK/terminal commit, duplicate stops and two
   control workers preserve one budget/fence and do not admit a new writer.
5. Independent ingress and egress loss while provider work continues: stale or
   missing ingress, empty successful polls, sender idle/429, another topic's
   success and unavailable notice delivery cannot fabricate control or receipt.
6. Shutdown during fenced interrupt and real SQLite writer contention after ACK
   preserve matched settlement with one RPC; pre-call deadline refusal quiesces
   only the sender. Refused acceptance retains exact identity without later
   upgrade. An ambiguous legacy topic cannot stop an unrelated final delivery.

The offline native witness passed 129 sequential deny-only pairs, exact final
retention and independent exact interruption of work surviving primary
connection loss. Scripted two-worker tests cover both durable results and
telemetry with 129 pairs per turn and 3,600 foreign frames. Schema48 tests cover
late claims, competing send owners, replaced claims, terminality without sender
quiescence, withheld completion retention and embedded parity. Its native
witness uses the real Hub journal for send-start and matched ACK; this is offline
evidence. Final publication gates, item 5 and live
two-worker acceptance remain open; source tests must not be presented as
installed Telegram acceptance. Live runs need a separately
approved exact candidate, rollback and bounded harmless scenario. Retain all
attempts, results and unknown delivery; never compensate by productive replay.

The native control-loss helper's immediate reread may still show active work.
The witness separately polls to prove eventual interruption; it does not prove
production's single reread always establishes terminality.
