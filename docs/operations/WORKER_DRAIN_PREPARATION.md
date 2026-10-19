# Worker operations drain preparation

Status: internal barrier plus default-off local control candidate; stronger
maintenance proof and live acceptance remain open. Owner: Hub maintainer.
Last verified barrier source: `4eb9b53`; local control is a stacked source candidate.
Next trigger: exact-candidate independent review and publication, then prepare the
separate native/client/history/custody boundary before any maintenance deployment.

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

The local source interface is owned by
[REQ-QUEUE-015](../product/DURABLE_QUEUE_AND_CONTROL.md) and
[ADR 0070](../decisions/0070-worker-operations-drain-control.md).
Installed workers gain no endpoint without explicit deployment opt-in.
Before a live use, the maintenance owner must close/verify cached clients;
verify exact native/control terminality and preserved history; and keep independent
restoration custody until original-process death and replacement convergence.
Timeout, stale acknowledgement or lost custody must refuse activation without
force or an inferred success. Existing uncertainty and human approval ownership
remain governed by the [queue contract](../product/DURABLE_QUEUE_AND_CONTROL.md)
and [security requirements](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Explicit local candidate workflow

An authorized immutable worker may be launched with `--drain-control-socket` and
a fresh socket path inside an existing private canonical directory. Enabling it
on an installed worker is a separate deployment task. A development checkout
without verified build identity refuses opt-in. Do not place the endpoint inside
provider-accessible project mounts or infer same-UID isolation from permissions.
Startup never removes a stale path. Preserve it and choose a fresh path or obtain
separate exact reconciliation authority.
The host must support descriptor-relative, no-follow chmod on Unix sockets;
unsupported platforms refuse opt-in startup and leave the default worker path
unchanged. Cleanup checks the created inode immediately before unlinking; these
are separate calls under the trusted same-UID assumption, not an isolation
guarantee against concurrent hostile same-UID replacement.

The local client has no configuration discovery or credential/state reads:

1. `worker-control inspect SOCKET` prints the observed binding. Independently
   compare it with the planned unit/process/release and private runtime marker.
   Save this exact output in an owner-held mode-0600 file; discovery alone does
   not select a maintenance target.
2. `worker-control prepare BINDING_FILE --request-id NONCE --ttl-seconds 30`
   prints a bounded request, without connecting or mutating. NONCE is a fresh
   32-character lowercase hexadecimal identifier. Save the exact output privately
   before transmission; never regenerate a request after an uncertain response.
3. Only with independent restoration custody and exact maintenance permission,
   `worker-control request SOCKET REQUEST_FILE` sends that request once. An
   accepted response may still report `operations_drained: false`.
4. `worker-control status SOCKET REQUEST_FILE` observes the original digest
   without mutation. Retain the exact response and its local observation time in
   the maintenance evidence store. No automatic retry, wait loop, target refresh,
   signal or activation follows. Errors return code 2 with an unconfirmed result.

Expiry after acceptance does not resume work. Losing the socket or observing a
new PID/nonce invalidates observation of the old request; it does not prove death,
completion or authority to restore. The in-memory acknowledgement is not a durable
maintenance ledger; private evidence persistence remains the maintenance owner's
responsibility. A receipt never replaces the stronger prerequisites above.

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
