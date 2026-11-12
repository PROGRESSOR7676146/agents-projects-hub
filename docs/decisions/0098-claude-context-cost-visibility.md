# ADR 0098: Claude context cost visibility and one-hour prompt cache

Status: accepted, 2026-11-11. Source implementation is separate from installed acceptance.

## Context

A Hub Claude turn resumes one native session per topic. On a proxy route the
CLI treats the connection as API-key use and writes the prompt cache with a
five-minute lifetime. Agents waited for long checks in foreground commands
(a Hub turn cannot keep background work), so the next request often came after
the cache expired and rewrote the whole conversation. With a session that grew
over a day, these rewrites dominated usage, and the owner saw neither context
size nor rewrites until the account rejected requests. Every such rejection was
reported as an exhausted account usage limit, although a 429 on this route does
not say which limit applied.

## Decision

1. Claude turns get `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` by default. The service
   environment may set another supported value; Hub only fills a missing one.
   A one-hour write costs more than a five-minute write, so the gain depends on
   reuse after five minutes; installed evidence decides whether it stays.
2. The worker observes usage per native request: the latest usage snapshot per
   main-thread assistant `message.id`, never the result's cumulative totals.
   A turn summary keeps request count, subagent request count, last and maximum
   request context (input + cache read + cache write), cache writes with their
   5m/1h split, cache reads, output, and the number of suspected large cache
   writes (a write above half of that request's context, at least 50k tokens, so a new session's first write is not flagged).
   Missing fields stay unknown; a request without usage, a compaction after the
   last request, or requests beyond the tracking bound make the current context
   unknown. Only these numbers are kept.
3. The summary is saved in the existing bounded `safe_metadata_json` of the
   turn's result, so no schema migration is needed. The reply footer shows
   `Context: 238k (last request)`, the count of large cache writes, and, from
   150k tokens, a note suggesting `/new` for the next task. `/status` shows the
   last request context of the session's latest finished turn; a failed,
   stopped or uncertain latest turn, or a turn without the summary, shows none.
4. A 429 without a native rejected rate-limit event naming a known account
   usage window (five-hour, weekly or overage) becomes `claude_rate_limited`:
   the notice says the route did not identify the limit or its reset.
   `claude_quota_exhausted` remains for such a named rejection. Both
   keep the worker's existing limited state until its next success; neither
   retries automatically.
5. The Claude transport note asks agents to keep one task per session, end a
   large task with a short handoff for `/new`, keep tool output short, and wait
   for a long check with one bounded command instead of repeated polling.

A Hub button that creates a new session with an automatic handoff is not built:
it needs a model call or an automatic context transfer that REQ-CTX-002
superseded. Auto-compaction thresholds wait for installed telemetry.

## Consequences

- The owner sees context size and large rewrites on every Claude reply.
- The large-write count is a diagnostic heuristic: expiry, prefix changes and
  compaction all produce it, and the summary does not claim a cause.
- Results committed before this change, recovered results and failed turns have
  no summary, so their context stays unknown.
- Hub still cannot tell a short-term rate limit from the account usage limit on
  a route that emits no rate-limit events.
