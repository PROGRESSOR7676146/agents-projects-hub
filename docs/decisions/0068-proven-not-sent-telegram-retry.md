# ADR 0068: Proven-not-sent Telegram delivery retries

Status: bounded retry scope; exact source review/publication belongs to its PR.
Date: 2026-10-14.
Owner: Hub maintainer; product decision owner: repository owner.
Verified source base: `525f105`.

## Context and decision

The conservative policy in [ADR 0060](0060-final-and-progress-delivery-certainty.md)
parks an attempted send without a positive receipt unless Telegram rejects it.
Initial DNS/TCP connection failures transmit no HTTP request, but an exception
class alone does not prove their phase: the same error can occur after a
redirect, proxy CONNECT, write or read. The owning contract amendment is
[REQ-QUEUE-005/013](../product/DURABLE_QUEUE_AND_CONTROL.md).

The default urllib transport owns one fresh in-memory witness per call.
Both HTTP and HTTPS handlers wrap native connection creation and send, sharing
the witness across all redirects and proxy steps. Only an OSError from initial
socket creation before any HTTP send produces the typed not-sent error. Native
TLS verification, proxy handling, redirects and request timeouts remain in the
standard library. The API adapter binds projection to the exact default opener
captured for that request; a custom opener or retry wrapper cannot supply proof
by raising the same private error type. A custom opener has no inferred phase proof. The witness is
not a reusable permission, persistent control or exactly-once delivery claim.

The shared classifier returns `proven_not_sent`, `proven_rejected` or `unknown`.
Final text/documents in embedded and external senders, progress and independent
task notices use their existing retry transactions, attempts and backoff.
The send-start fence still commits before transport; only a proven failure
allows its existing retry operation to clear it. Multipart prefix receipts and
spool references stay intact. There is no schema change or second transaction
owner. Receipt-commit faults never use the transport classifier.

The small transport module owns only native attempt evidence. The Telegram API
adapter owns bounded error projection; delivery helpers own classification use;
existing domain facades and HubState retain SQL/transaction ownership. Provider
invocation and artifact cleanup owners are unchanged. No hotspot receives a new
responsibility, so a broad Controller/state extraction is unwarranted here.

## Limits and next trigger

Crash before saving the proven retry, expired/stale ownership, TLS failure,
write/read failures, HTTP5xx, malformed success and uncertain receipt commit
retain the existing unknown policy. Neither current network recovery nor this
source change releases historical unknown rows. Notice FIFO bypass, ledger
dispositions and session/root/control authority remain separate work; this
increment implements none of them. Hermes PTB notice certainty remains unchanged.
Existing attempt caps and notice-obsolescence rules still apply: retries consume
attempts, and exhaustion follows the existing terminal delivery-failure policy.
This increment adds no unlimited outage retry or stale-notice policy.

Offline tests use real urllib/http.client orchestration with fake sockets and
temporary SQLite state. They must cover initial DNS/refusal/timeout, text and
documents, post-connect ambiguity, redirects, proxy CONNECT, native TLS settings,
persisted cooldown after restart, prefix/spool retention and independent notices.
Canonical exact-revision gates and independent review precede publication.
Live deployment/Telegram acceptance needs separate owner authorization.

Next trigger: exact-source review/publication, then owner integration and a
separately authorized rollout. This source increment may close when gates pass;
publication does not establish live recovery or complete the wider delivery work.
