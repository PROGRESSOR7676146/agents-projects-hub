# ADR 0046: Topic-wide emergency stop

Status: accepted
Date: 2026-09-27

## Context

REQ-CMD-007 stopped only the topic's active agent: it cancelled that agent's
queued jobs and interrupted its running turn. A provider invoked by mention
while another agent was active kept running and kept its queue. The first live
acceptance baseline at `ea5af70` showed it: a mentioned Codex turn continued
after `stop`, because an earlier check had made Antigravity active. An owner
who types "stop" in a topic expects the work in that topic to stop.

## Decision

On the owner's decision (2026-09-27), an emergency stop applies to the whole
numeric topic. It cancels every queued or retry-waiting job in the topic,
whatever its provider, and addresses the stop request to the provider of the
running turn. Topic FIFO allows at most one running turn, so one request row
per Telegram message still suffices and no schema change is needed; when
nothing runs, the request is addressed to the active agent and completes at
once, as before. Held jobs awaiting an owner decision are left for that
decision.

A stop covers exactly the work that existed when it was recorded, except work
then held for an owner decision. The stop takes its time under the write lock,
and a job takes its creation time inside its own admission transaction, so
"existed" means committed before the stop. Every stop check uses this one rule:
the start of a turn, the start of a same-turn steering follow-up, the worker's
stop monitor, its checks after the provider returns or fails, and the choice of
the job that carries the Hub acknowledgement. An older unfinished stop
therefore never interrupts or cancels later work, and a repeated stop message
never attaches a notice to a job that started after the stop, including a held
job the owner confirmed after it. The acknowledgement can be attached to a
covered job of any provider.

A leased job counts as running. Moving it to `executing` checks for a covering
pending stop inside the same immediate write transaction: a stop committed
first cancels the job without invoking the provider, and a stop committed later
finds the job executing and interrupts it. A steering follow-up is started the
same way: a follow-up the stop covers is cancelled without the steering call,
and one it does not cover returns to the queue when a stop covers the parent or
the parent turn has ended. No follow-up is leased into a turn that a pending
stop covers, or while it is held for an owner decision. Cancelling stopped work
completes, in the same transaction, every pending stop that covers it once none
of that stop's covered work is still queued, leased or running. A stop
therefore outlives neither its work nor a worker that dies after the
cancellation, and it never ends while a covered follow-up, leased or returned
by a rejected steering call, could still start.

## Consequences

`stop` is predictable regardless of which agent is active. The existing
worker interruption path, the no-replay rule and the durable Hub
acknowledgement are unchanged. Other topics are unaffected. Rollback to a
schema-35 release remains possible because the schema is unchanged.
