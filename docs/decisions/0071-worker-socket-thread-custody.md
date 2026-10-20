# ADR 0071: Separate worker socket transport thread custody

Status: source candidate; publication, deployment and live acceptance pending.
Owner: Hub maintainer. Date: 2026-10-19.
Contract: [REQ-QUEUE-015](../product/DURABLE_QUEUE_AND_CONTROL.md).

## Context and decision

The operations barrier in ADR 0070 deliberately excludes persistent transport
threads. Putting those threads into it would prevent drain while the idle
primary connection remains cached. Detachment after an unconfirmed close also
cannot prove that an abandoned transport physically exited.

Reuse `WorkerActivityBarrier` separately inside the existing Codex supervisor.
One supervisor helper supplies its tracked thread factory for every socket
construction, including idle probes. Constructor operations are fenced by the
same owner, and reservations survive uncertain startup or failed initialization.
The existing barrier checks physical liveness after the target returns. No
client registry, background task, service, database or schema is added.

After admitted worker operations and their cleanup physically finish, the main
productive owner closes supervisor client admission and closes its eligible
cached socket client once. The empty pre-startup refusal does the same. It never
closes an injected supervisor or a stdio peer for this proof. A stuck loop or
unknown startup keeps the observation pending indefinitely; observation never
repeats cancellation. An ordinary stop/exception exit does not manufacture a
retirement receipt.

Only a supervisor constructed exclusively by the worker has eligible custody.
Managed-server ownership, injected instances/subclasses and any selection of
stdio are permanently unsupported. A monotonic event records ineligibility
before its effect; a later socket restoration cannot erase it. Admission refusal
precedes startup/mode mutation and must never enter stdio fallback.

## Protocol, ownership and exclusions

The default-off endpoint becomes strict version 2 with a separate
`socket_threads_drained` scalar. Old frames/files are refused without fallback.
Its true value requires an accepted exact request and operations drain, on both
server and client. The scope names tracked operations and socket transport
threads separately. Unsupported ownership is null; an eligible pending proof is
false. Before acceptance it is always null.

The listener only closes operations admission and observes in-process state.
Its explicit getter does no filesystem I/O, SQL, client close or provider action.
Barrier observation may prune dead thread references under its short memory
lock; it does not mutate durable state. Worker/control/state owners retain all
invocation, transaction, settlement and cleanup responsibilities. No queued work,
delivery, native evidence, approval or replay policy changes.

The field covers this supervisor's socket transport threads only. Client helper
threads, cached-object closure, other supervisors/processes, peer/server state,
exact native terminality, durable control settlement and history are excluded.
Direct socket constructors used by independent monitor/local/Controller callers
in this process are outside this supervisor's observation as well.
A matched native response can still fail persistence; thread exit cannot replace
its committed quiescence evidence. Independent native, history and restoration
custody checks remain required before any service action.

## Verification and closure

Fictional fixtures must cover detached/unconfirmed cleanup, failed construction,
startup uncertainty, idle-primary retirement without operations deadlock,
unsupported ownership/fallback and protocol contradictions. Publication requires
exact-source independent review and canonical gates. No installed endpoint or
live maintenance acceptance follows from this source candidate.

Next trigger: finish source tests and independent review, then prepare the
separate durable/native/history/custody acceptance guards. This ADR remains open
until publication evidence and separately authorized live evidence exist.
