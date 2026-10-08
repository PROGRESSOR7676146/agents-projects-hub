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
[REQ-QUEUE-011](../product/DURABLE_QUEUE_AND_CONTROL.md).

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

## Offline directional sequencing and partial-write buffer follow-up

Add a separate single-owner `review_bridge_sequence.py` for wire ordering;
leave the codec policy-free. Host initialization is SPEC then CAPSULE. The child
may report bounded native stdout before its single REQUEST, and one NATIVE_EXIT.
The host response is one HEADERS, bounded CHUNK frames, then END. Separate
per-direction wire/frame budgets count headers and empty frames; response body
and native stdout have independent aggregate limits. These payloads stay opaque:
no native HTTP schema, exit status, stream terminal or authorization is inferred.

Host CANCEL revokes later issue. Already in-flight child stdout may drain within
its original budget, but the returned discard disposition prohibits publication.
Child requests after cancellation refuse, including an already in-flight request
reported as a local ordering fault without submission; child exit ends the drain. EOF closes
only its own direction. An exit before response END may close both pipes but
retains incomplete-response and sticky request-observed evidence. Request seen
is neither callback consumption nor provider acceptance; a late ordering fault
cannot reset an already consumed gate. Future workflow ownership must combine
these distinct observations conservatively rather than equating callback return
or closed pipes with completion.

`review_bridge_write_buffer.py` owns finite immutable encoded frames, temporary
capacity backpressure, cumulative admission bounds and one outstanding write
offer. The caller reports an exact bounded advance; even zero progress requires
a nonempty outstanding offer, and leaves it
unchanged. Backpressure refuses admission without spending either ledger. A
bounded response constructor accepts exact fixture bytes, never an arbitrary
iterator. There is no blocking writer, I/O callback, thread, clock or process.
Enqueue attests buffer admission, advance only caller-reported bytes, never peer
receipt. After buffer cancellation or failure, the future I/O owner must close
the pipe unconditionally and never reuse it. In particular, a wire CANCEL cannot
be appended to a truncated frame suffix. Graceful wire CANCEL instead requires
preserving prior frames. Missing frame attributes refuse through fixed errors,
permanently retire the primitive and retain its earlier observation counters.

Sequence and buffer remain separate primitives. A future serialized owner must
coordinate admission and order before any physical write, abort on invalid order,
and never advance sequence when buffer admission returns false. Fake-owner tests
cover this backpressure boundary, partial headers/payloads, in-flight cancelled
stdout, unfinished EOF phases and no callback replay after a late sequence fault.
This is still in-process evidence, with no claim of interruptible transport.

Next add an owned nonblocking pipe runner and namespace supervisor/HTTP witness
with a fake upstream; independently enforce deadlines and descendant cleanup.
Productive wiring
still requires durable role/session/result/material/lease bindings, host-held
route credentials, exact native request/response validation, process-tree stop
and cleanup, independent review and separately authorized live acceptance.
