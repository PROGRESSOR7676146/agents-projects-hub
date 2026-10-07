# ADR 0058: Bounded pipe primitives for isolated review

Status: offline implementation candidate; productive integration pending.
Date: 2026-10-08.
Owner: Hub maintainer.

## Context

The [private namespace](0056-provider-neutral-process-namespace.md) deliberately
cannot reach host loopback inference or authority endpoints. The
[sealed material capsule](0057-sealed-review-material-capsules.md) contains only
explicitly selected bytes. Restoring shared networking, mounting a host socket
or passing arbitrary descriptors would undermine that isolation. The initial
advisor still requires durable role/material authorization under
[REQ-WRITER-013](../product/ACCOUNTS_CONTROL_AND_SECURITY.md) and
[REQ-QUEUE-011](../product/PERSISTENCE_AND_RECOVERY.md).

## Decision

Prepare a narrow stdin/stdout pipe transport between a trusted worker and a
future immutable in-namespace supervisor. The supervisor may eventually expose
an isolated loopback HTTP endpoint to the native CLI; host credentials and
upstream selection stay with the trusted worker. Do not change the namespace
mount/network contract or add a daemon, host socket, inherited capability FD,
provider-neutral framework or production runtime entry point in this slice.

Implement only dependency-neutral byte framing and a separate in-memory fake
attempt gate. `review_bridge_protocol.py` owns the versioned header, fixed frame
types, per-frame bounds, bounded feed/total bytes and frame count. It checks a
declared body against its bounds before accumulating it, counts headers and
empty frames, and permanently retires after malformed input or EOF. It does not
interpret provider requests, direction, order or authorization. Raw payloads and
invalid caller fields are excluded from frame repr and fixed error diagnostics.

`review_bridge_attempt.py` receives a frozen host-created spec, a caller-owned
sealed capsule and an explicitly injected fake callback. The spec has bounded
opaque identity/material binding, exact capsule digest/size, canonical native
UUID, selected model/effort/output limit and the trusted expected request digest.
There is no default upstream, URL, credentials, project path, environment lookup
or network client. Capsule reading verifies seals and canonical material bytes;
the gate copies only immutable bytes, retaining no host descriptor ownership.

Only a request frame with the exact trusted byte digest can claim the attempt.
Child spec/capsule/response frames cannot create or replace host policy. Matching
a capsule digest beside arbitrary request text is insufficient. This first gate
does not interpret native HTTP semantics: the trusted caller owns preparation
and authorization of the exact request, including its semantic relationship to
spec metadata and selected materials. Native body validation and compatibility
need a separate adapter and witness; fake exact-body tests cannot establish them.

The lock owns validation and the one-use transition before callback invocation.
The callback runs outside the lock, so concurrent/reentrant submissions cannot
claim again. Refusal, cancellation before claim and admission expiry retire the
gate without a call. Any callback outcome consumes the attempt; an exception or
unavailable/revoked/expired response remains bounded local uncertainty. Callback
diagnostics are not retained in error cause/context. A successful callback return
is only local transport evidence, never provider acceptance, terminality,
product success, cost or a right to replay. Cancellation after claim cannot
promise absence of a call or remote cancellation/refund.

Host monotonic time fixes a finite deadline at construction. It limits admission
and result eligibility and refuses invalid/backward clock evidence. It cannot
interrupt a synchronous injected callback. An injected clock is trusted, fast,
side-effect-free and nonrecursive: admission reads it under the local lock.
The future owned I/O pump must enforce
process/transport deadlines independently; this gate is not that process runner.

## Ownership, evidence and next trigger

Integration owner: Hub maintainer. Framing owns byte validation; the attempt gate
owns only local one-use admission; the trusted caller retains capsule lifetime.
No SQLite transaction, provider process, worker lease or result publication owner
changes. Creating another gate after a crash loses local consumed state and does
not authorize replay. Durable authorization/deduplication remains with the future
workflow transaction owner. There is no queue/config/service enabling switch.

Unit fixtures use real sealed material bytes and recording fake callbacks. They
cover fragmentation, declared-size and aggregate bounds, EOF retirement,
material/request substitution, forbidden frame directions, concurrent/reentrant
claim, callback errors, invalid host input and before/after-claim cancellation
and deadline behavior. These are in-process offline evidence, not a pipe crossing
a namespace, native inference, subscription routing, deployed custody or Telegram
acceptance. The [testing guide](../testing/README.md#offline-review-pipe-primitives)
owns the commands and evidence limits.

Next add explicit directional sequencing and bounded response pumping, then an
owned namespace supervisor/HTTP witness with a fake upstream. Productive wiring
still requires durable role/session/result/material/lease bindings, host-held
route credentials, exact native request/response validation, process-tree stop
and cleanup, independent review and separately authorized live acceptance.
