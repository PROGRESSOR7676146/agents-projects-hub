# ADR 0049: Task visibility and stop certainty

Status: accepted; implementation in progress
Date: 2026-10-02

## Context

The owner accepted visible queue, approval and execution waits in the next
development plan. A fake-provider test also reproduces an unsafe interaction:
a stop request plus a lost response becomes `cancelled`, even when the exact
provider turn remains active or cannot be read. Another topic can then acquire
the same root. ADR 0046 explicitly permitted this uncertainty cancellation;
that part of its decision is superseded here.

The old stop acknowledgement occupies the unique provider-result outbox row.
Preserving an uncertain failure would then collide with that acknowledgement.
Control delivery therefore needs an independent record within the existing
SQLite database and sender, not a new service.

## Decision

The owning contracts are [REQ-QUEUE-012 and REQ-QUEUE-013](../product/PERSISTENCE_AND_RECOVERY.md#implemented-queue-compatibility-and-local-provider-worker-isolation).
An owner stop is an intent. An interrupt RPC acknowledgement or owned-process
termination alone does not prove the native turn terminal. Unknown execution
remains indeterminate and retains root exclusion, without productive replay.
Exact completion, failure or interruption may establish terminality. A result
that loses to a covering stop is still suppressed under ADR 0046; proof and
visible output cancellation are separate facts.

Stop intent, cancellation of unstarted covered work and its source-topic
acknowledgement commit together. Independent control notices preserve event
identity and receipts without replacing provider failures. A send is marked
durably before the network call. Recovery may retry an unattempted lease;
an attempted send without a recorded positive receipt becomes unknown.
Only a proven API rejection permits delivery retry. Unknown delivery neither
repeats provider work nor changes root ownership.

The complete visibility scope includes queue blockers, approval waiting,
meaningful progress, active-work retries and honest stop scope. The accepted
300/1,200-second thresholds only notify. Implementation proceeds in bounded
slices; the first stop/notice slice does not claim the remaining scope complete.

## Ownership and architecture review

The lead development agent owns integration and schema. `StopState` owns the
single stop transaction; the notice facade participates in it without a nested
commit. The provider-job facade owns execution leases and root exclusion.
Workers alone invoke providers, and the existing sender alone contacts Telegram.
Notice delivery has no provider client and cannot change execution state.
Exact-turn observation remains read-only at the provider boundary. Cleanup of
temporary provider materials remains worker-owned; an uncertain native turn
is never made retryable by cleanup.

Extracting stop state from `HubState` reduces its responsibility and size.
New schema SQL lives in a separate module; released migrations remain unchanged.
The bounded exception for migration registration and Controller flag wiring is
owned by the repository maintainer, limited to schema registration and atomic
stop-notice selection, reviewed again by 2026-12-31 or on any further lifecycle
branch. No additional responsibility is added to the main update dispatcher.

## Migration and acceptance

Schema 36 adds independent notices and preserves legacy Hub stop outbox rows
and every multipart receipt in private archive tables before moving them.
An attempted or partially delivered legacy notice becomes unknown, never a
fresh send. Unknown legacy shapes fail migration rather than discard evidence.
Provider jobs, provider results and their outbox rows remain unchanged.
Exact completed-stop evidence extends the terminal-evidence vocabulary without
pretending the provider was interrupted. Migration is covered by the existing
backup and atomic rollback discipline; runtime rollback requires an artifact
declaring schema-36 compatibility.

Required evidence includes fake active/unknown exact reads after interrupt
success and failure, embedded/external queue regressions, no second same-root
writer, atomic stop/notice fault rollback, positive receipt validation,
retry-after persistence, ambiguous-send recovery, migration preservation and
exact completion after an uncertain stop. Tests and review are repository
evidence; Telegram and native provider acceptance require separate authorization.
