# ADR 0019: Durable Codex execution checkpoints

Status: repository implementation; deployment acceptance pending
Date: 2026-09-11

## Decision

Schema 22 adds `provider_execution_checkpoints` and `provider_visible_items`.
The journal is private task data, not diagnostic telemetry. It stores the
canonical root and thread identity before sending a productive turn, accepted
turn identity before consuming events, completed visible assistant items, and
the completed response before formatting/spooling/outbox commit. Binding the
provider session early preserves the newly-created thread even when work fails;
the existing job admission snapshot remains immutable.

Every write requires the current unexpired executing lease. Item IDs deduplicate
records, conflicting identity/content is rejected, and only commentary,
final-answer or legacy unknown visible phases are accepted. Each job retains at
most 512 visible items and 200,000 item-text characters; its completed response
is separately bounded to 200,000 characters. Exceeding a bound fails explicitly.
Reasoning, tool output and raw terminal data are never journal items.

Before generic stale-job recovery, Codex consumers atomically claim one expired
invocation with a recovery-only lease. This does not increment invocation
attempts or make work queued. The same reconciliation also runs after a handled
post-acceptance transport or result-commit failure while its original lease is
still valid. A durable completion checkpoint can be committed to the normal
result/outbox without any provider access. Otherwise an accepted thread/turn
identity permits one read-only lookup. Only the exact completed turn under the
same canonical project root is eligible for recovered success.

The official [Codex app-server contract](https://developers.openai.com/codex/app-server/)
defines `thread/read` with `includeTurns: true` as reading stored data without
resuming or subscribing. Recovery uses that stable method, never `thread/resume`,
`turn/start` or synthetic inference. Missing/unsupported history, identity/root
mismatch, an unfinished turn or ambiguous acceptance becomes an indeterminate
notice with saved partial text where the binding remains valid. It does not
search other sessions or guess the most recent turn.

Both isolated and embedded Codex queue paths use the same journal/recovery
implementation. Inline legacy paths outside the durable queue are not included.
Previously validated staging files of a confirmed completed job pass through
the existing spool checks on recovery; partial jobs do not auto-publish files.

## Limits

Provider acceptance can precede receipt/persistence of the turn ID. That window
remains uncertain, and recovery must not guess or replay. A remote turn still
running at reconciliation is not treated as completed. Each failure boundary
makes one bounded read attempt rather than introducing an unbounded recovery
poller. Data not observed/persisted before the failure can only be recovered if
the exact completed provider turn is available. Protocol read failures cannot
become successful empty responses. Stdio reads have a queue-backed deadline and
WebSocket closure wakes both blocked directions.

## Migration and rollback

The additive migration shares the existing SQLite backup/transaction/integrity
gate. Older binaries whose maximum schema is 21 must reject schema 22. Ordinary
runtime rollback must use a distinct artifact explicitly supporting schema 22;
restoring an old backup would discard accepted work and is not a runtime rollback.
The rehearsal now checks the candidate artifact's target schema rather than
hard-coding schema 21. Fictional schema-22 artifacts carrying the real migration
exercise activation and rollback; a schema-21 rollback is rejected by the manifest.
These fixtures do not establish that a production rollback artifact is ready.

## Evidence

`tests/test_execution_journal.py` covers additive migration and injected DDL
rollback; abrupt child-process exit before accepted identity, after acceptance,
after partial text and after completion; no provider reinvocation; exact-turn
read recovery; wrong root/thread and unfinished turns; lease fencing, item
idempotency, phase and storage bounds; and compatible artifact rehearsal.
The existing fault matrix, migrations, queue, sender and privacy checks remain
mandatory. Live deployment/Telegram acceptance is a separate task.
`tests/test_result_reliability.py` additionally covers exact-turn recovery after
a handled disconnect and aggregate passive outcome telemetry.
