# ADR 0061: Local owner delivery-hold dispositions

Status: accepted design; source implementation under validation.
Date: 2026-10-08.
Owner: Hub maintainer; product decision owner: repository owner.

## Context

Unknown final/notice delivery conservatively parks a begun Telegram send. The
[delivery-certainty prerequisite](0060-final-and-progress-delivery-certainty.md)
therefore cannot be activated without a bounded way for the owner to let the
topic continue. Changing unknown to delivered or failed would invent certainty
or hide evidence. This decision implements the owning
[REQ-QUEUE-005](../product/PERSISTENCE_AND_RECOVERY.md), without another sender,
job state, role workflow or outcome assessment.

## Decision and ownership

Schema44 adds one immutable `telegram_delivery_hold_dispositions` table, with
foreign keys to the retained target and no cascading deletion. Database triggers
reject updates and deletion. It records local owner authority, exact outbox/job,
nullable saved result, immutable destination/binding, action, snapshot version
and SHA-256, and application time. It carries no Telegram actor identity.

The `delivery-hold` CLI previews by default using `open_read_only`; explicit apply
uses `open_existing`. Both refuse missing/older state without initialization or
migration and use no private deployment configuration, credentials, transport or
provider. Local authority relies on the existing trusted OS/state boundary;
this CLI adds no separate human authentication or isolation from an unconfined
same-UID model process. Deployed custody remains an independent activation gate.
The snapshot is a stale-state check, not an authentication secret or remote authorization.

HubState owns a coherent read transaction for preview and one immediate write
transaction for apply. The small DeliveryHoldState helper owns target validation,
full ordered-part snapshot and insertion. An exact retry returns the recorded
decision even after an uncertain commit; a conflicting token cannot overwrite it.
The full manifest is hashed without a reference-page cutoff. Output exposes only
bounded target identity, counts, action and tokens, never content or paths.
Preview distinguishes the fresh target token, the original disposition token
and whether the recorded permission still matches the binding.

Two dependency-neutral predicates own only topic delivery/FIFO exceptions:
`outbox_delivery_hold_released` and `job_blocks_topic_fifo`. Sender leasing,
provider leasing/fairness, stalled-work diagnostics, queue snapshots, typing and
steering use them consistently. Unknown remains ineligible for send. Only an
exact result_ready job with its saved result can be skipped as a productive FIFO
predecessor. Unreleased unknown successors still hold their own topic barriers.

General busy/drain, root, writer, stop, pending owner-hold, new/model/agent,
local/return, connect/adoption, relocation and inline guards stay strict. Thus the
owner may continue already authorized queue work while the existing session and
writer safety checks still apply. Passive totals preserve unknown; separate
counts distinguish outstanding and released holds. Outcome/status/retry copy
does not imply delivery, provider acceptance or product assessment.

## Verification and limits

Focused fixtures cover all-part/provenance stale snapshots, exact retry,
competing SQLite connections, transaction faults, both FIFO barriers, native
uncertainty, local writers, pending owner holds, stop, consistent diagnostics,
read-only CLI, credential-free apply and populated43-to44 backup/DDL rollback.
Fictional older fixture builders temporarily supply an empty schema44 table to
the current FIFO queries, then verify emptiness and remove it before migration
or backup assertions. Production migrations have no such compatibility bridge.

Publication requires full gates and independent exact-candidate review. Live
acceptance must verify continuation and evidence preservation on the exact
authorized release. No deployment, resend, provider replay, remote owner control,
assessment, scoring or productive collaboration is enabled by source publication.
