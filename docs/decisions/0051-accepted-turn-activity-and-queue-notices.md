# ADR 0051: Accepted-turn activity and queue notices

Status: accepted; repository implementation under validation
Date: 2026-10-02

## Context

[ADR 0049](0049-task-visibility-and-stop-certainty.md) separates control delivery
from provider results. Its remaining visibility work needs observable queue and
execution evidence without interpreting worker heartbeat as provider progress
or treating a timeout as permission to release a root. The existing SQLite
queue, provider event stream and Telegram sender already own these boundaries.

## Decision

The owning contract remains [REQ-QUEUE-012/013](../product/PERSISTENCE_AND_RECOVERY.md#implemented-queue-compatibility-and-local-provider-worker-isolation).
Admission records acceptance and the current queue blocker in its existing
transaction. Duplicate input and batched continuation preserve the original
job and notices. Queue categories are immutable snapshots, at most once per
category per job; they are not a continuously refreshed scheduler view. Capacity
claims require actual scheduler configuration. Numeric owner-topic links may
identify the blocking topic without exposing a root path. Execution handoff
records a separate notice; it is not proof of native turn acceptance.

For Codex, a worker installs the activity callback before `turn/start`, persists
the exact accepted turn through the execution journal, then binds activity
before consuming buffered events. The activity facade owns its transaction;
explicit in-transaction methods participate in an existing transaction without
committing it. The binding includes the current execution lease, project and
scope, numeric topic, Hub session generation and agent, Telegram writer, native
thread/turn and checkpoint root. A shared passive guard checks the same binding
when recording activity, evaluating a deadline and beginning first delivery.
A completed checkpoint or changed binding suppresses an obsolete unsent notice.

Only bounded, payload-free observations are durable: tool transitions and output
activity, completed visible items, and exact approval request/resolution identity.
The combined metadata bound is 512 entries per job. Existing entries may still
resolve at that bound. Worker heartbeat, typing and provider retries are not
progress. Tool output has no stable provider delta identity; scoped observations
advance the clock without storing bytes or claiming exactly-once delta accounting.

The existing sender passively evaluates ordinary/tool deadlines and prepares
one notice per no-progress episode. Meaningful activity re-arms the episode and
supersedes its never-attempted warning. Approval waiting prepares a generic
human-action notice and pauses ordinary/tool warnings; resolving the exact
request restores the mode of the remaining active tools. Concurrent approvals
remain waiting until all observed requests resolve. These observations never
answer an approval, call a model, stop a turn, change its lease or authorize replay.

Only the configured Hub identity in external queue/external outbox mode enables
this delivery. First-send validation and suppression share the notice transaction.
The existing sender retains its fair delivery classes and schema-36 uncertainty
rules: attempted delivery without a positive receipt stays unknown, and only a
proven Telegram rejection authorizes retry. Database deduplication is not an
exactly-once Telegram guarantee.

## Ownership, migration and limits

The lead development agent owns integration and schema registration. Queue
admission and lease facades own their transactions; activity and notice facades
own their domain writes. The worker owns provider invocation and callback cleanup
on start, binding and wait failures. The sender owns Telegram delivery and has
no provider invocation authority. Shared binding validation depends only on
SQLite, not on worker, Controller or sender orchestration.

Schema 37 adds activity tables and preserves existing jobs, result outboxes,
control receipts and unknown sends. Migration uses the existing consistent
backup and atomic DDL rollback. A runtime rollback must explicitly support
schema 37; schema-36-only binaries cannot open the upgraded database.

This slice does not complete every requirement in ADR 0049. Approval that blocks
`turn/start` before an exact accepted turn exists remains the native host's
responsibility and cannot receive an accepted-turn notice. Active-work retry
controls and broader provider/compatibility-path visibility remain separate work.
Claude human approval hosting, tools, read-only advisor isolation, native local
transfer and full parity are not enabled by Codex activity observation.

## Evidence required

Offline tests cover exact binding changes, duplicate and out-of-order events,
parallel tools/approvals, restart persistence, metadata bounds, atomic rollback,
custom deadlines, first-send staleness, unknown delivery, and zero productive
invocations from sender evaluation. Migration tests preserve queued/executing
jobs and attempted unknown notices through upgrade and injected DDL failure.
Worker tests verify installation before start, binding after acceptance and
cleanup after start/wait failures. Canonical validation and required independent
review must name the final candidate revision. Deployment and native Telegram
acceptance remain separately authorized; this ADR reports neither.
