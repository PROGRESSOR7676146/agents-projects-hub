# ADR 0018: Visible results on handled provider failure

Status: implemented; durable follow-up in ADR 0019
Date: 2026-09-11

## Problem

A provider can emit useful visible output and then fail. The Codex client used
to discard that output when raising a generic error, while the queue emitted
an identical uncertain-outcome notice for preparation errors and accepted work.
Notifications buffered while an RPC was outstanding were never read by the
turn waiter. Separately, Telegram cooldown hints were ignored by both senders.

## Decision

Consume buffered notifications before reading the transport. Deduplicate visible
completed items by ID. A failed turn wait retains only completed `agentMessage`
text in a typed exception, bounded to the latest 16,000 characters with an
explicit omission marker. Reasoning and tool items are never eligible.

Both queue paths persist that excerpt as an explicitly incomplete failure notice
through the existing outbox. Publishing the notice neither creates a successful
provider result nor acknowledges the interaction contract or visible context.
Known rate-limit, disconnection and timeout causes use fixed safe wording; raw
provider diagnostics remain outside Telegram. A known cause does not prove that
the task had no side effects. Uncertain productive work is never replayed.

Caught setup failures before the `turn/start` call get a terminal preparation
failure notice, rather than a claim that productive work may have started.
This first stage retains the conservative existing durable invocation boundary:
process death during preparation can still become indeterminate.

Both sender paths use the same persisted retry delay: exponential backoff from
one second, capped at five minutes, or a larger valid Telegram `retry_after`.
No process sleeps while holding a delivery lease for cooldown. Deterministic
backoff is sufficient for the current single-owner sender; random jitter is
deferred until concurrent-sender load provides a reason to add it.

## Durable follow-up

This first change made no schema update. ADR 0019 subsequently added schema-22
thread/accepted-turn identity, durable per-item progress and exact-turn read-only
reconciliation for abrupt process death and handled post-acceptance failures.
The remaining uncertainty window before the accepted turn ID is persisted still
cannot authorize replay.

## Evidence

`tests/test_result_reliability.py` exercises buffered completion, duplicate items,
visible text followed by failure/EOF, incomplete outbox delivery without a
successful job transition, preparation rejection, compatibility paths, bounded
escaped excerpts and cooldown persistence across sender restart. Existing queue,
worker and sender suites continue to cover no automatic productive replay.
These are fictional offline tests, not deployed provider/Telegram acceptance.
