# Worker operations drain preparation

Status: internal runtime foundation; external maintenance control and live
acceptance remain open. Owner: Hub maintainer. Source base: `200616c`.
Next trigger: independently review this source, then implement an exact-incarnation
local request/receipt boundary before proposing any maintenance deployment.

## Purpose and extraction

An idle heartbeat precedes recovery and turn observation. It cannot establish
that the worker's metadata or late-control threads are idle. Cooperative SIGTERM
and timed joins likewise cannot establish physical thread termination.

`worker_activity_barrier` owns a dependency-neutral, process-local admission gate
and operation/thread accounting. ExternalQueueWorker retains startup, productive
execution, cleanup and its SQLite connection. CodexControlMaintenance retains its
independent connection and claim/settlement transactions. CodexLiveControl retains
native stop/steer and client ownership. No transaction or provider policy moves
into the barrier. There is no new service, database or schema.

This extracts the shared admission/accounting lifecycle rather than duplicating
it in each loop. Worker integration only wraps existing whole passes and wires
existing thread creation; it does not reimplement recovery or control policy.

## Current in-process boundary

`request_drain()` monotonically closes admission on one worker object. It does
not set its stop event. Already admitted cycles, including their bounded nested
work and cleanup, finish normally. New primary, metadata and maintenance passes
are refused before state claims or client creation. The metadata and maintenance
loops exit and close their own state. The productive loop remains parked until
an explicit stop. No queued job is cancelled, released or replayed by drain.

Owned thread activity is retained before Thread.start and released only after
its target's finally blocks. Actual thread liveness also blocks `drained` after
target return. A startup exception retains its reservation until target exit;
if no target exit is established, drain remains unconfirmed. A join timeout
does not remove activity. The barrier tracks metadata, maintenance, heartbeat,
external stop-monitor and accepted Codex live-control threads. Starting a child
of already admitted work remains possible after drain; background threads do
not inherit permission to start new passes. Normal embedded control keeps its
existing behavior when no external-worker barrier is supplied.

`activity.drained` proves only closed admission and completion of these tracked
operations/threads. It does **not** prove exact native terminality, durable control
settlement, absence of cached clients or transport-owned threads, preservation of
old work, or authority to stop a process. It must never be substituted directly
for a native-quiet callback or a maintenance permission. Constructor side effects
precede this boundary; a request before run_forever prevents runtime startup only.

## Remaining maintenance integration

The current change adds no CLI, signals, socket, request file or durable receipt.
No process can remotely drain a stock installed worker using this API alone.
Before a live use, an independently reviewed exact-incarnation control must bind
the request and acknowledgement to the worker epoch; close/verify cached clients;
verify exact native/control terminality and preserved history; and keep independent
restoration custody until original-process death and replacement convergence.
Timeout, stale acknowledgement or lost custody must refuse activation without
force or an inferred success. Existing uncertainty and human approval ownership
remain governed by the [queue contract](../product/DURABLE_QUEUE_AND_CONTROL.md)
and [security requirements](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Verification and closure

Offline tests use fictional workers, real temporary SQLite and controlled threads.
They cover refusal before all claims, normal productive completion with a queued
tail, a blocked database close, a live-control join timeout and a real matched late
interrupt settlement that finishes after drain. They invoke no provider, Telegram,
installed service or production state. Full source gates and independent review
remain required for publication; deployment/live evidence is separate.

This preparation remains open until the remaining maintenance boundary has its
own source and separately authorized live evidence. Passing the operation tests
does not close native channel-loss acceptance.
