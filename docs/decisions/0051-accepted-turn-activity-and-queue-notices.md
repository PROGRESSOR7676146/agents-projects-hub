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

Schema 40 adds a separate, bounded observation scope for approvals seen during
`turn/start`. Three additive tables retain prepared-checkpoint scopes, typed
request metadata and monotonic runtime epochs. A runtime registers once for its
configured external-worker slot; creating observations never registers another
epoch. Startup atomically retires older open scopes and only their unattempted
notices. Heartbeat remains telemetry. Each write and first unattempted send checks
the exact prepared lease/session/root/destination and explicit profile context.

On a shared socket, a same-thread early turn ID cannot establish acceptance of
the submitted task. The common early/accepted copy therefore reports a request
observed in the Codex session. Early IDs never enter the execution journal or
authorize interruption, approval or replay. After journal acceptance, one
transaction binds ordinary activity, imports only matching pending/resolved
requests and promotes the scope before buffered events drain. Resolution and
retirement cannot erase attempted/unknown transport evidence. Promoted scopes
use exact accepted proof rather than the runtime epoch. The first-send guard
allows a matching accepted checkpoint during the small journal-to-promotion gap;
stale early provenance never falls through to an unrelated accepted alias.

The early bound is 128 requests per scope; physical early plus accepted metadata
remains bounded by 512 rows per job. Both phases share one notice key and immutable
copy. Native `never` declines do not advertise a human wait. Copy does not promise
that `/stop` interrupts an unaccepted submission. Native approval ownership and
schema-36 send completion/recovery remain unchanged. Schema-39-only binaries
cannot open the upgraded database; upgrade and rollback retain the existing
consistent backup and migration transaction.

This slice does not complete every requirement in ADR 0049. Broader provider and
compatibility-path visibility remain separate work.
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
