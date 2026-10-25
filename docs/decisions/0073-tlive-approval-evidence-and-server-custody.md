# ADR 0073: Passive card evidence and independent Codex server custody

Status: source candidate; independent review and live acceptance pending
Date: 2026-10-25

## Decision

The owning behavior is [REQ-OPS-005](../product/PERSISTENCE_AND_RECOVERY.md).
The permission router projects only bounded pending native identities, delivery
state and times through the existing local IPC. Its random epoch starts anew
with each daemon. The bootstrap returns the actual adapter delivery reference;
queue admission, onPending, a dashboard event and Hub lifecycle-notice delivery
cannot establish card delivery. Ambiguous sends remain unknown. Resolution or
connection loss retires pending projection; a late send result cannot recreate it.
Snapshot output is capped at 128 entries and 100,000 entry bytes, contains no
input, path, card body, chat identity or callback capability, and marks truncation.
The protected Claude permission namespace and first-valid-answer-wins stay intact.
The upstream router's long request method receives only request-owned observation
fields and send settlement; a separate delivery state machine would duplicate its
pending-map authority and resolution races. Bootstrap only returns the existing
adapter receipt and exposes one read-only switch case. This bounded source-patch
exception is owned by the Hub maintainer, reviewed again by 2026-11-25 or upon
another lifecycle/authority branch; independent exact-source review is required.

HubState owns the coherent read transaction. The separate
`approval_surface_state` domain checks existing accepted and prepared bindings;
`approval_surface` owns bounded read-only IPC and pure evaluation. The monitor
receives aggregates only and uses existing edge-triggered alert delivery. No
schema, control authority, provider invocation, replay or automatic repair is added.
The small HubState forwarding method does not justify splitting its transaction
owner. Exact-source independent review remains required for this bounded hotspot
exception; owner: Hub maintainer; next review: 2026-11-25; reopen on any new
projection, control or transaction responsibility.

The optional external-server unit owns one pinned Codex process independently
of tlive. Attach-only mode observes the configured socket but never spawns or
stops a server. Neither service binds its lifetime to the other. Start requires
fresh local evidence, exact artifact pins and a clean immutable Hub release.
The reusable start guard refuses existing sockets or servers, active/unknown
Hub work, unsettled native control ownership and local writers. It never removes
sockets or kills processes. A 60-second receipt is a local operator assertion
and exact manifest binding, not Telegram authentication or native idle proof.
Standalone clients must be closed by the operator; the guard cannot infer them
from a green doctor. No autonomous migration, restart, state repair or resume is
provided. `Restart=no` keeps server crashes subject to a new explicit start gate.

## Evidence and limits

Carried Node fixtures exercise native event interleavings, subscription metadata,
receipt/ambiguity, daemon epochs, bounds and attach-only reconnect. Python tests
exercise persisted identity, aggregate evaluation and start refusals. The build
path executes carried offline fixtures; CI stages the SHA-pinned release first.
The release archive omits upstream tsconfig and plugin-lock tooling. These tests
and a build do not establish full upstream CI/typecheck, installed services,
Telegram card visibility, native history preservation or recovery of old controls.
Migration, exact clean revisions, restart E2E and rollback remain separately
owned under the [operator procedure](../operations/TLIVE_RELIABILITY.md).
