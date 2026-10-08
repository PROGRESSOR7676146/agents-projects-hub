# Codex control-loss acceptance and recovery

Status: staged source fix; full control-loss acceptance pending
Owner: lead development agent. Last verified base: `2f8e8bf`.

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
| Interrupt ACK | Request acknowledged; independently read exact terminal state |
| Active/unknown exact turn | Root remains held; no retry, reset or replacement writer |
| Prepared notice | Saved for delivery; owner receipt is not established |
| Aggregate Telegram transport failures | Diagnose ingress/egress separately; exact-topic loss is unproved |

Do not resolve uncertainty merely to unblock a root, restart an active worker,
stop the shared server or reset queue rows. Use a separately authorized exact
native stop or independent recovery channel when the late-stop handler is not
available. Recheck terminal state before any new same-root work. Restart alone
cannot close the buffer/control incident.

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

The offline native witness passed 129 sequential deny-only pairs, exact final
retention and independent exact interruption of work surviving primary
connection loss. Scripted two-worker tests cover both durable results and
telemetry with 129 pairs per turn and 3,600 foreign frames. Items 4/5 and live
two-worker acceptance remain open; source tests must not be presented as
installed Telegram acceptance. Live runs need a separately
approved exact candidate, rollback and bounded harmless scenario. Retain all
attempts, results and unknown delivery; never compensate by productive replay.
