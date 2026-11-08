# ADR 0093: Hub-owner recovery of an unknown delivery hold

Status: accepted design; source implementation under validation.
Date: 2026-11-08.
Owner: Hub maintainer; product decision owner: repository owner.

## Context

An attempted final Telegram send can remain `unknown` after a timeout. The
result is safely saved, but its topic's FIFO waits behind the unknown delivery.
ADR 0061 deliberately made the first recovery path a local, read-only
preview/apply command. That remains valid, but it makes a narrowly understood
recovery unavailable from the Hub topic where the warning is visible.

Treating a Telegram press as local OS authority would be incorrect: the two
trust boundaries are different, and a same-UID process does not authenticate a
Telegram actor. Resending the result, replaying the provider turn, changing the
delivery status, or manufacturing a receipt would all hide the uncertainty
rather than recover the queue safely.

## Decision

Schema56 adds a separate Hub-originated, immutable remote-provenance ledger.
It leaves `telegram_delivery_hold_dispositions`, its rows, foreign keys and
immutable triggers intact. The new records use only the authority value
`telegram_owner_callback`; local CLI records remain `local_owner_cli`, and
neither path can overwrite or relabel the other.

The capability exists only when the configured Hub bot is the established
external queue/outbox ingress and has an explicitly configured, positive
numeric Telegram bot ID as well as its configured username. A current
configured owner may Reply with the exact `/delivery` command to the delivered
Hub delivery-wait warning in the same numeric topic. Telegram's reply and
callback message authors must match both configured Hub identifiers; the
numeric ID is retained with the capability and immutable decision. Missing,
changed or malformed Hub identity disables this remote path. Acceptance actors,
direct chats, forwarded or augmented commands, another bot's warning, and an
unavailable queue topology do not fall through into productive routing or
another recovery path.

The Hub sends one content-free preview with one opaque 24-character callback
capability. It is usable for ten minutes and is bound to the configured owner,
numeric chat/topic, exact warning and preview message IDs, Hub message origin,
outbox/job/result/session binding, all ordered parts, receipt/artifact evidence
and the complete deterministic snapshot. The state retains only its digest.
It records the capability only after Telegram positively returns the preview
message ID. An ambiguous preview send is deliberately unusable and is never
resent automatically.

On the button press, the existing Hub-Bot update path verifies the configured
owner ID and that the callback message was authored by the configured Hub bot.
One `BEGIN IMMEDIATE` transaction rechecks all actor/chat/topic/message/origin
bindings, expiry, target eligibility and the complete snapshot before appending
the remote disposition. It rejects stale, expired, cross-user, cross-chat,
cross-topic, cross-message and conflicting callbacks. A queued/repeated callback
from the same bound owner returns the original immutable decision without
inserting another record. The visible preview and callback responses omit paths,
digests, deployment data, target IDs and the capability value.

The sole effect is to exempt that exact saved-result unknown delivery from the
topic FIFO predicate. The outbox stays `unknown`; parts, receipts, artifacts,
job status and provider history stay unchanged. No callback has resend, provider
replay, Reply, assessment, approval, stop, writer, native-terminal, session,
root, workflow, capacity or full-control authority. ADR 0063 full-control
consent remains independently local and retains its own binding checks.

## Persistence and compatibility

Schema56 is additive: callback-token and remote-disposition tables have
non-cascading foreign keys and UPDATE/DELETE refusal triggers. Conflict guards
prevent a local and remote queue-only disposition from being recorded for the
same outbox. The remote predicate additionally requires exact token provenance,
so damaged remote rows do not release FIFO. Topic binding remains frozen unless
the existing independently authorized full-control condition applies.

The normal migration backup and atomic DDL transaction protect populated
schema55 state. Legacy dispositions are not rebuilt. Schema56 executables are
required after migration; rollback needs a schema56-compatible artifact and
never authorizes removing later accepted remote records.

## Verification and lifecycle

Offline fixtures cover the real Hub command/callback ingress, configured-owner
checks, actual Hub-bot callback origin, exact warning Reply, malformed numeric
identities, expiry, stale all-part/receipt snapshots, fault rollback, duplicate
and concurrent callbacks, restart after preview binding, FIFO continuation and
immutable populated schema55 upgrade/DDL rollback. They do not contact Telegram
or a provider.

This ADR is source behavior only. Exact-candidate independent review, canonical
and hosted gates, compatible rollback, separately authorized deployment, and
owner Telegram acceptance remain pending. No source test is delivery proof.
