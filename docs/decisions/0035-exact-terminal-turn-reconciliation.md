# ADR 0035: Exact terminal Codex turn reconciliation

Status: repository implementation; deployment acceptance pending
Date: 2026-09-24

## Context

A transport disconnect can follow a visible partial response and an accepted
Codex turn. The Hub cannot equate that disconnect with either completed work or
an active provider turn. An indefinite root exclusion after a proven provider
failure blocks an owner-requested continuation, while automatic replay can
duplicate file and external effects.

## Decision

Schema 34 records provider terminality separately from the old job's
`indeterminate` outcome. Only an exact read of the saved thread, turn and
canonical root can establish `completed`, `failed` or `interrupted`. Completed
output is published from persisted provider data. Failed/interrupted evidence
allows a new, notice-bound owner request in the same session, with an instruction
to inspect current state before continuing. The old job, checkpoint, error and
notice remain historical evidence; they are never marked successfully completed
or reset for replay. Unknown and active turns retain root exclusion. A bounded
observation queue repeats only read-only checks.

Queued requests already present on that root are held and visible until a
separate owner decision. A continuation reply cannot consume them. The
Controller accepts `retry` only as a Reply to the delivered failure notice;
SQLite enforces one continuation job per source job. The worker refuses a
continuation if its transport would create another provider thread.

An explicit `reconcile-existing-local` operation can align an already opened
native session with Hub ownership. It requires the exact Hub session, provider
thread, generation, origin, root and old job, an idle terminal provider read,
and an owner assertion about the CLI boundary. A standalone CLI must first be
closed at idle; a remote CLI must be idle. PID absence is not proof. The
operation changes only writer ownership and, when needed, appends terminal
evidence. It starts no CLI or provider turn.

## Ownership and recovery

The HubState SQLite connection owns each immediate transaction. Provider reads
occur outside transactions. The worker owns read-only observations and provider
invocation; the Controller owns Telegram admission and Reply binding; the
sender owns delivery. A sender lease blocks replacement of an uncertain notice
until its attempt settles. The prior notice is archived before a corrected
notice or recovered result enters the outbox. The observer snapshots staged
artifacts for a proven completed turn and removes unused snapshots if its state
commit loses a race; the sender owns delivered spool cleanup. The same root remains excluded
until exact terminal proof or a separately reviewed operator resolution.

Migration 34 adds terminal evidence, queue holds, continuation identity,
bounded observation state and an archive of replaced notices. It preserves
schema 33 data and requires a release whose schema gate supports 34. Runtime
rollback to a schema-33 executable is unavailable after migration; no downgrade
or database rewrite is part of ordinary rollback.

Offline fake app-server and SQLite tests cover exact turn status, partial
failure, deduplication, held work, transfer guards and no productive diagnostic
call. Live Telegram/provider acceptance remains separate.
