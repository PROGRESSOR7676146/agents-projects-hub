# ADR 0031: Bounded concurrency across independent roots

Status: repository implementation; deployment acceptance pending
Date: 2026-09-13

## Decision

Schema 32 completes Package F on top of the schema-31 canonical-root exclusion
contract. `max_parallel_roots` is a global configuration limit for simultaneous
Hub-owned provider execution. It defaults to one, accepts 1–16, and values above
one require external queue mode. Each configured provider worker still owns one
SQLite connection and one adapter, client, or supervised process lifecycle, so
worker count is an additional upper bound. The design does not add shared
clients, thread pools, or multiple turns inside one provider worker.

Lease admission counts distinct unexpired `leased`/`executing` scopes in the
same `BEGIN IMMEDIATE` transaction that chooses and fences the next job. When a
slot is available, fresh configured workers compete by durable
least-recently-granted sequence, then oldest eligible job. The polling worker is
always considered; other workers require a cached heartbeat no older than two
minutes. This gives a continuously ready live worker a bounded turn while a
stopped worker cannot reserve the scheduler indefinitely.

Lowering capacity does not cancel or interrupt active work. Each polling worker
durably advertises its configured value; fresh workers use the lowest advertised
capacity, so a rolling reduction takes effect after the first restarted worker
polls and then drains without an over-capacity grant. A rolling increase remains
at the old lower value until old declarations are replaced or age out. New
leases wait until occupancy is below the effective value. Expired pre-execution leases cease to
occupy capacity and are safely requeued by existing recovery. Expired execution
becomes `indeterminate`: unresolved uncertainty continues excluding its own
canonical root but does not consume a global slot or block an independent root.
Emergency stop remains job/token scoped and releases only that job's slot.
The filesystem slot releases after the productive result transaction commits;
`result_ready` is Telegram-delivery ownership and performs no provider or
project mutation, so it does not hold the root across other topics. Existing
strict FIFO still prevents the next job in that same topic from passing its
undelivered final-result boundary.

An explicit worktree lane becomes executable only after a local bind of an idle
numeric topic. Bind and archive atomically update both lane metadata and topic
execution scope and reject queued, active, local-writer, or unresolved work.
Immediately before provider access, the worker proves that the lane path is the
derived sibling for its registered project, lies under a canonical allowlisted
root, is listed by Git as a linked worktree, and reports itself as Git top level.
The same resolver supplies that root to external and embedded execution,
staging, local resume-command preparation, and read-only Codex recovery; a
missing or mismatched retained binding fails before provider access or writer
transfer. Local resume and non-Codex return capture topic/session/generation and
lane identity, validate the filesystem outside SQLite, then recheck that snapshot
inside the ownership transaction. Queued summary admission and writer transfer
commit together; invalid roots or changed snapshots leave local ownership and
session identity unchanged with no new summary job. Codex return remains
model-free and does not add a filesystem or provider call.

Legacy managed-terminal takeover, productive inline Codex/native/pilot consumers and
inline local-summary paths reject an active lane before session preparation,
staging, ownership transfer or provider access, including bindings retained
after switching runtime configuration. Embedded/external lanes remain supported.
Archive restores a conservative project-ID fallback; the next registry-aware
topic observation upgrades it to the canonical base root. Cleanup remains a
separate explicit local operation.

Cache-only status reports configured capacity, occupied/available totals,
bounded worker instance/agent/phase owners, and an aggregate unresolved-scope
count. It exposes no project root, topic, prompt, provider session, output, or
credential identity.

## Alternatives rejected

- A global process mutex cannot represent capacity above one or durable
  fairness and would serialize unrelated roots.
- A thread pool inside one provider worker would share SQLite/client/process
  ownership across turns and make targeted shutdown and recovery ambiguous.
- Topic identity or a lane metadata row alone does not prove filesystem
  isolation; only an explicitly bound and execution-time-validated worktree is
  an independent same-project scope.
- Counting unresolved uncertainty as global occupancy would let one ambiguous
  provider outcome stop every unrelated project. Releasing its own root would
  instead risk duplicate writers, so exclusion remains root-local.
- Cancelling active work when capacity is reduced would cross the established
  conservative execution boundary and create a configuration change as authority
  to stop provider work.

## Migration and rollback

The unmerged development branch used earlier migration/ADR numbers. Integration
reserves released schemas 26–30 for connect, provisioning, editing and provider
provenance; this decision uses an additive migration after schema 30. Released
migrations are unchanged. A prerelease database from the divergent branch is
not a supported released-schema input and must not be relabelled to force an upgrade.

Migration 32 adds durable grant/capacity advertisements and a partial unique index that
permits at most one active lane binding per topic. Existing lane rows are
retained. A legacy database with conflicting active bindings fails the migration
transaction rather than choosing a winner. Capacity stays in configuration and
defaults to one, so an omitted key preserves serialized execution. Runtime
rollback requires an artifact whose maximum supported schema is at least 32;
the additive table and index are retained.

## Evidence

Offline tests cover capacity one/two, draining reduction, fairness and stale
worker removal, independent progress after crash-to-uncertainty, targeted stop,
same-root contention, passive owner projection, real worker cwd in a linked
worktree, invalid sibling rejection, idle-only bind/archive, migration
preservation, conflicting-binding rollback, and existing queue/process fault
boundaries. This is repository evidence only. Enabling capacity above one,
restarting services, and live Telegram/provider E2E remain an explicit private
deployment task.
