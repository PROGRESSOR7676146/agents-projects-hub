# ADR 0070: Exact-incarnation worker operations-drain control

Status: accepted source design; deployment and live acceptance remain separate.
Owner: Hub maintainer. Date: 2026-10-19.
Contract: [REQ-QUEUE-015](../product/DURABLE_QUEUE_AND_CONTROL.md).

## Context and decision

The in-process operation barrier closes admission without cancelling admitted
work. Maintenance needs to address the selected incarnation while the productive
thread is busy. An idle health sample cannot establish that boundary. Non-Codex
workers have no independent metadata loop: a mailbox inspected between productive
passes may remain unobserved throughout a long turn and expire before admission
closes. A signal handler cannot safely acquire the barrier's ordinary lock.

Use a default-off private Unix socket and one bounded listener inside the existing
worker process. No new service, schema, database, polling mailbox or dependency
is introduced. Finite inspect/request/status exchanges avoid a blocking wait that
would monopolize the listener. The existing barrier remains unchanged: polling
its physical-thread-aware property avoids a new condition variable whose last
notification could precede actual thread exit.

One exact request is retained in memory for the incarnation. Losing a reply is
handled by observing the saved request, without a new mutation. A fresh process
cannot inherit acceptance; expiry affects only first acceptance. Kernel peer
identity, start ticks, boot identity, worker nonce and verified release prevent
an old binding from silently following a replacement process.

The endpoint refuses every existing socket path. Holding a slot lock does not
prove custody of an arbitrary configured path shared with another namespace.
The owner may select a fresh path or separately reconcile stale evidence; startup
never repairs it automatically. Directory descriptors pin lookup and cleanup;
cleanup verifies the endpoint's own socket inode immediately before unlinking.
This check and unlink are separate system calls; concurrent hostile same-UID
replacement is outside the stated trusted-local-authority boundary.

## Ownership and architecture review

`worker_drain_protocol` owns bounded data validation and process identity.
`worker_control_socket` owns its directory/socket/listener and single in-memory
request. It calls only the dependency-neutral `WorkerActivityBarrier`; it imports
no state, worker, Controller or provider orchestration. `worker_control_cli` owns
worker argument registration and the explicit local client. This cohesive parser
extraction reduces the main CLI parser rather than adding socket policy there.
The CLI owns startup and cleanup under `worker_process_lock`, before releasing
the slot. Default-off construction does not inspect the worker or filesystem.

Existing state/domain facades remain sole transaction owners; invocation, claims,
settlement, clients, runtime nonce and final cleanup remain with their current
worker/control owners. This protocol cannot replay a job or settle a durable claim.
Failed or lost transport leaves drain unconfirmed; accepted closure is monotonic.
No listener join timeout is physical exit proof. No barrier condition, SQLite
transaction, native RPC, signal or approval path is added.

## Evidence and remaining boundary

Temporary real sockets, independent dummy processes and actual fictional worker/
SQLite fixtures cover busy admission, queued-tail preservation, exact status after
reply loss, expiry, process replacement, peer identity, malformed input, bounded
I/O, path custody and cleanup. Fixtures invoke no installed service, Telegram or
provider. Source tests and independent review do not establish installed custody.

An operations-drained response proves only tracked operation/thread completion.
Cached transports, exact native terminality, durable control settlement, history
and independent restoration custody still require their own proof. Do not wire
this receipt directly to a native-quiet predicate or process-stop authorization.
