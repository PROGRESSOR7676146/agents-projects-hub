# ADR 0055: Managed Codex permission-profile continuity

Status: accepted implementation slice; live acceptance pending
Date: 2026-10-06

## Context

Legacy workspace overrides can replace a native named permission profile.
Passing the profile only in a worker's current configuration also lets a restart
silently change already accepted work. Native Codex 0.159.2 exposes profile
selection and managed allowlisting, but intentionally hides managed definitions;
the active profile's parent can be explicitly null despite an inherited managed
definition. Metadata therefore cannot establish complete filesystem custody.

## Decision

[REQ-SEC-001](../product/ACCOUNTS_CONTROL_AND_SECURITY.md) owns the observable
contract. Use a dependency-neutral selection validator and an explicitly supplied
immutable state context. Distinguish omitted context from explicit legacy null.
Keep selection snapshots in sessions, jobs, execution checkpoints and connect
workflows. Schema 39 adds nullable columns and immutable-update triggers without
retargeting historical rows. The existing migration transaction and private
backup remain the upgrade/rollback owners.

The state/session/admission facades retain their existing transaction ownership.
The sender checks connect authorization before marker publication and again in
the atomic activation transaction. Continuation copies the checked source
selection. The worker records server confirmation before productive submission.
The app-server client owns bounded metadata checks, preparation notification
buffering and post-submission drift interruption. Selection remains independent
of model/effort and source-provider provenance.

The supported first route is an external Codex queue worker, on the shared socket
or exact stdio resume. Managed steering, inline/pilot and local/tmux execution
are refused before effects. Unsupported routes do not gain authority merely
because a socket exists. Refusal preserves access to passive diagnosis and an
explicit new generation. Explicit provider switching may select a stale
satellite for control only, retaining its generation/model/effort/selection;
productive admission remains refused until owner-confirmed `/new`. Promotion of
a historical local/terminal satellite preserves its writer and returns before
implicit model replacement; the owner can then return/release that writer.
Domain resets
cannot archive a local or terminal writer. Ownership-only Codex `/return` may
release a historical local writer without rewriting its selection or invoking
a provider.

`codex_permission_refusals` owns the input-refusal transaction and reuses the
existing Hub input-notice outbox without blocker fields or jobs. It atomically
commits a fixed `/new` notice and input receipt before downloads or preparation.
The typed enqueue refusal is checked again after a raced admission failure.
Repeated input stays declined across restart and `/new`; uncertain Telegram
sends retain the existing transport semantics, not exactly-once delivery.
Notice-bound continuation under changed permissions uses the same atomic refusal
and receipt, with no continuation job. A failed refusal retains the input for
redelivery. Ownership return points to `/new` when the saved selection is stale.

## Evidence and consequences

Fake transports cover malformed/ambiguous selection, pagination bounds, early
and accepted-turn drift, and visible policy broadening. Temporary SQLite tests
cover restart, immutable snapshots, context changes and migration rollback.
The optional real-native offline corpus additionally executes a diagnostic
command through the actual Hub client before and after app-server restart and
exact resume with `never`. Its fake provider and isolated network do not exercise
real inference, Telegram approval transport or a deployed authority boundary.

The corpus requires a completed command in the exact current thread and turn,
with positive project read/write controls and negative fictional authority,
symlink and Git-write controls. A skipped corpus is not native evidence. The
fixture does not expose host credentials and declines unexpected approvals;
it is not evidence that a live approval was delivered or accepted.

Remaining work includes deployment-specific custody, all helper/tool launch
paths, supported local transfer, advisor isolation and owner-coordinated live
acceptance. A matching profile ID alone never closes those gaps.
