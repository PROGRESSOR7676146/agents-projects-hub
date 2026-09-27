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
decision. The stop acknowledgement can now be attached to a cancelled job of
any provider, but only to work that existed when the stop was recorded, so a
duplicate stop message never attaches a notice to a later job.

A leased job counts as running. When its worker moves it to `executing`, the
same transaction honors a pending stop recorded after the job was created: the
job is cancelled without invoking the provider and the stop completes. An older
unfinished stop, for example after a worker crash, never cancels later work.

## Consequences

`stop` is predictable regardless of which agent is active. The existing
worker interruption path, the no-replay rule and the durable Hub
acknowledgement are unchanged. Other topics are unaffected. Rollback to a
schema-35 release remains possible because the schema is unchanged.
