# Codex control-loss acceptance and recovery

Status: staged source fix; full control-loss acceptance pending
Owner: lead development agent. Last verified Stage 1 revision: `fe7bb51ffa8dc491a33c624614dcff57ab40208d`.
Stage 2 source published at `b9822a797572ee6edab0d3b3c515516e38a68148`;
no deployment acceptance. Stage3's schema49 prerequisite is source-published at
`7af0204a117e2b1a23c0a364f44249c12de14fa4`; schema50 provenance and schema51
causal episodes are source-published at
`4727241b3906c27ab4556cd4abd8eb6157e0c4f6` and
`506f3bf765ae477bc1945c6317214a3d86fcec6c`. Schema52 authority and local-write
prerequisite are published; runtime precautions are under exact-source validation.

Follow [REQ-QUEUE-014](../product/DURABLE_QUEUE_AND_CONTROL.md) and
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
| Outbound queue expiry after calling the client | Unknown sender remains fenced; no late frame or automatic resend |
| Active/unknown exact turn | Root remains held; no retry, reset or replacement writer |
| Prepared notice | Saved for delivery; owner receipt is not established |
| One unknown commentary/progress delivery | Retain uncertainty; this alone does not authorize interruption |
| Aggregate Telegram transport failures | Diagnose ingress/egress separately; exact-topic loss is unproved |
| Fenced successful group poll | Recent global ingress; exact topic control is unproved |
| New ingress epoch with historical success only | Startup uncertainty; no current poll or automatic control |

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
the live invocation allows three fresh no-send cycles five seconds apart. A
send fence or exact terminal proof ends those retries. This in-memory count is
not the persisted late-cycle budget. If all cycles fail, native work may continue
until natural completion; the pending stop still withholds publication and no
stopped outcome is inferred. Idle late-control polling takes
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
   One unknown progress with healthy ingress must not interrupt. With independent
   due ingress loss, the same unknown progress or delivery consent must not hide
   that cause. Recovery before reservation must suppress an unsent precaution;
   recovery after reservation must retain its fence and sender ownership.
6. Shutdown during fenced interrupt and real SQLite writer contention after ACK
   preserve matched settlement with one RPC; pre-call deadline refusal quiesces
   only the sender. Refused acceptance retains exact identity without later
   upgrade. An ambiguous legacy topic cannot stop an unrelated final delivery.
   Block the outbound WebSocket writer behind another frame, expire the original
   proof cutoff and resume it: the interrupt must never start writing and its RPC
   waiter must wake. Repeat after response timeout, close-before-dequeue, full
   queue and unsupported transport. Separately prove an uncompressed on-time
   local socket write with an ACK after proof expiry, inside the response budget.
   Post-client-call expiry keeps an unknown owner/fence and cannot authorize
   another interrupt or release the root. These are offline transport witnesses,
   not physical delivery timestamps or installed Telegram acceptance.

The offline native witness passed 129 sequential deny-only pairs, exact final
retention and independent exact interruption of work surviving primary
connection loss. Scripted two-worker tests cover both durable results and
telemetry with 129 pairs per turn and 3,600 foreign frames. Schema48 tests cover
late claims, competing send owners, replaced claims, terminality without sender
quiescence, withheld completion retention and embedded parity. Its native
witness uses the real Hub journal for send-start and matched ACK; this is offline
evidence. Stage2 publication gates passed; item5 and live
two-worker acceptance remain open. Source tests must not be presented as
installed Telegram acceptance. Live runs need a separately
approved exact candidate, rollback and bounded harmless scenario. Retain all
attempts, results and unknown delivery; never compensate by productive replay.

The native control-loss helper's immediate reread may still show active work.
The witness separately polls to prove eventual interruption; it does not prove
production's single reread always establishes terminality.

Schema49 prerequisite fixtures cover group-only actual polls, startup CAS,
idempotent repeats, stale/competing publishers, sequence gaps, diagnostic and
commit faults, separate historical confirmation, and additive migration with
retained schema48 unknown-sender fences. The pure policy covers long healthy
turns followed by restart, immediate new-epoch recovery, original third-failure
deadlines, stale/missing samples and malformed clocks. These are offline state
and policy checks. Exact-target persistent episodes are now source-published in
schema51. The runtime witness below now covers native work surviving fictional
ingress loss; the ledger and pure policy alone grant no control or delivery authority.

The neutral transaction prerequisite covers assessment/reservation atomicity,
caller rollback, rejected commit, guard refusal and monotone real-stop binding.
It does not grant ingress control authority; its composed tests use an existing
independently authorized protective source. Follow the dated ADR amendment for
the accepted egress policy rather than treating delivery consent as ingress recovery.

Schema52's dormant state candidate covers reassessment plus immutable first-send
cause capture, recovery before/after reservation, current lease/read-claim guards,
one shared budget/fence and later real-stop precedence. The state fixtures also
cover bounded post-fence claims for read-only terminal/result observation after ingress or
native sends, with no resend, cause enrichment or sender quiescence. Recovery,
terminal proof and owner resolution suppress ingress claims; later stops retain
only the shared remaining cycles.
Its populated schema51
upgrade must preserve rows, claims, schedules, unknown senders and exact earlier
triggers, with a nullable added parent column, empty cause storage, consistent
backup and complete DDL-fault rollback. Historical sends must not acquire causes.
Runtime fixtures now exercise the shared accepted worker wait, progress/final
callbacks and steering through optional assessment/read/notice faults. They also
cover reservation-time stop priority, post-attempt state faults preventing
steering, no-send observer continuity and later stop withholding, real eligible
keyset rows beyond healthy/error pages with insertion/eligibility withdrawal,
post-fence raw completion with unknown delivery and sender ownership, notice
deduplication and receipt-commit ambiguity. The optional native ingress fixture
keeps the primary connection open while independent polling becomes overdue,
observes native active work, calls its real `_poll_ingress` method once and independently
proves eventual exact interruption. It uses one local scripted Responses call,
without real auth, Telegram, service changes or productive replay. Final
publication review/gates and item5's installed channel-loss acceptance stay open.

Under [REQ-QUEUE-014](../product/DURABLE_QUEUE_AND_CONTROL.md), authenticated
`not_sent` preserves live observation/steering but consumes the permanent send
fence: a later `/stop` cannot send again and retains completion withholding.
Notice preparation is best-effort; its immutable cause remains available to
bounded recovery explanations. No blind notice resend or extra scheduler is
introduced. Persistent real-stop claim faults retain connection reopen/backoff
and may delay ingress maintenance; investigate storage faults separately.

Schema50 provenance fixtures must additionally cover current Reply retry chains,
continuation duplicates, batching/steering's nine identity pairs, direct steering
recheck and stop precedence, refused/repeated acceptance, atomic storage faults,
and SQL REPLACE collisions with recursive triggers disabled. The additive
migration retains every schema49 row/object and unknown sender fence, creates
empty sidecars, and rolls back a DDL fault. These bindings do not detect loss or
authorize control; item5's durable episodes and send-start integration stay open.

Schema50 activation requires a distinct schema50-compatible runtime rollback;
the schema48 Stage2 runtime is incompatible. Roll back the executable against
retained current state. Never restore the pre-migration database over later
work, checkpoints or send fences to accommodate an older runtime.
