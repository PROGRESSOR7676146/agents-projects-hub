# ADR 0038: Multiple isolated Codex worker slots

Status: repository implementation; deployment acceptance pending
Date: 2026-09-27

## Decision

Codex may run up to 16 separately identified external queue processes, while
OpenCode and Antigravity retain one process per agent. The default remains one.
`codex_worker_count` bounds Codex leases independently of the global
`max_parallel_roots` limit. Each process owns its own SQLite connection and
Codex client. The existing Codex worker unit is slot one; a dedicated systemd
template starts numbered slots two and above. One process lock per slot prevents
duplicate CLI instances from overwriting health for the same identity.

The queue counts active Codex jobs inside its lease transaction, refuses a
second active lease for the same worker identity, preserves canonical-root
exclusion, and considers Codex eligible for fairness while spare Codex slots
remain and a fresh, unoccupied slot can actually poll. Every configured slot is
required by cached health and exact revision
convergence. Multiple slots require an external queue and a separately managed
Codex server; a worker cannot supervise the shared server for its peers.

The existing scheduler declaration table stores a separate global-capacity
advertisement for each canonical Codex slot. Readers also consider the legacy
agent-key declaration until it ages out. This preserves the lowest fresh limit
during a rolling global reduction without a schema change. A Codex slot-count
reduction stops surplus processes before changing the count; a config edit
cannot reconfigure processes already running with the old count.

## Ownership and failure boundary

`ExternalQueueWorker` owns one process/client lifecycle; the CLI owns its slot
lock and closes both lock and worker on exit. `ProviderJobState.lease` owns the
single SQLite admission transaction. Provider invocation remains after the
existing lease and execution-root validation. A lost pre-execution lease is
requeued; a possibly started turn follows existing Codex reconciliation and
indeterminate rules, never an automatic productive replay. A process crash
releases its OS lock, while durable job recovery remains with the state layer.
Focused queue, worker, health, CLI-lock and systemd tests establish repository
behavior; publication still requires canonical validation and exact-revision
CI, followed by separate live acceptance.

## Rollout and recovery

Raising a count does not start processes automatically. Configure and start the
numbered units explicitly, then verify all slot identities and exact revision.
For reduction, drain and stop surplus units before lowering the count and
restarting remaining workers. Existing turns retain their conservative
completion/recovery boundary. No SQLite migration is required; count and slot
identity are configuration and process state. The operational sequence lives in
[Queue recovery](../operations/QUEUE_RECOVERY.md#capacity-and-lane-changes).

Offline tests prove admission, per-worker and per-root exclusion, fairness,
duplicate process-lock refusal, and health/revision projection. Live parallel
Codex turns across distinct projects and rollback remain separate acceptance.
