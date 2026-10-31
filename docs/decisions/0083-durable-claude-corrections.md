# ADR 0083: Durable Claude corrections in one owned process

Status: source candidate; exact-source review and publication pending.
Date: 2026-10-30
Owner: Hub maintainer; sole implementation writer: primary development agent.
Contract: [REQ-ROUTE-010](../product/IDENTITY_AND_INTERACTION.md).

## Context and decision

The pinned native [characterization](../testing/CLAUDE_STREAM_INPUT.md) proves
that streamed stdin can deliver a correction during a tool loop, but a late
input can instead start another native turn in that process. Neither a pipe
write nor a replayed user message proves completed work. Interrupting a tool
to imitate a steer would add effects and lose the native boundary.

Keep one process, permission host and result owner. Limit this increment to one
compatible correction, then preserve normal FIFO. This bounds the lifecycle to
two receipts and at most two results without a persistent native-client broker
or permission-host retargeting. Multiple corrections and extensions are separate
increments; no new plugin, MCP, child agent or tool authority is introduced.

Schema55 adds an initially empty input-run ledger and one immutable parent-child
submission binding. It does not reinterpret Codex's native-turn absorption IDs.
The child's reservation is deliberately conservative: a crash after reservation
but before a write can retain uncertainty. Automatic replay would be less safe
because a crash after a write has the same missing local evidence.

The parent retains the existing invocation lease and cleanup boundary. Child
leases follow its heartbeat and do not consume another provider worker slot.
Only the parent participates in saved-completion recovery. Aggregate raw text
and input validation commit together; final outbox/child completion share the
existing result transaction. Failed or incomplete correction streams retain
the original inputs, visible partials and root exclusion. Owner resolution of
one uncertain job does not silently resolve a distinct uncertain child.

## Architecture ownership and extraction

`ClaudeCorrectionState` owns transaction-required ledger SQL and child settlement;
HubState remains the sole SQLite transaction/connection owner. It imports no
worker or Controller. `worker_claude_corrections` supplies callbacks on the worker's
existing SQLite thread and revalidates the registered root before reservation.
`ClaudeCorrectionInput` owns bounded framing and receipt/result ordering.
`ClaudeStreamReader` retains native session/tool policy and visible-message
checks before normalization. `ExternalCliAdapter` owns the one productive spawn,
nonblocking pipes, deadlines, process-group termination and cleanup. The existing
permission launch keeps its exact original job/lease/profile binding throughout.
No second monitor writes the input and no callback runs inference.

Optional admission failure seals further admission and preserves ordinary FIFO
only while no durable child binding exists. An ambiguous reservation commit
retains uncertainty. Model/effort selection checks govern new admission; they do
not invalidate receipts/results of the original owned invocation. Child queue
notices follow reservation and settlement in those same transactions.

The runtime, worker, migration runner and HubState hotspots receive forwarding
and transaction-local composition only. The new cohesive lifecycle lives in the
three modules above; extracting another facade would duplicate ownership. The
bounded entries in `hotspots.json` must be independently reviewed before source
publication. Reopen on multiple corrections, extensions, changed permission
ownership or invocation/recovery behavior; next review: 2026-11-13.
Any new parent terminal path also reopens the settlement audit. Existing bulk
stale recovery is covered with a synchronized submitted pair; it retains both
inputs as uncertain. Cross-table terminal enforcement is not added by this ADR.

## Verification and activation

Failure-focused tests cover reservation rollback, missing/conflicting receipts,
same/next result boundaries, child persistence faults, process exit conflicts,
FIFO/stop/selection/provenance refusal, saved-result settlement, capacity, stale
leases, immutable linkage and additive migration/DDL rollback. Real subprocess
fixtures use fictional output, no account or inference. Native characterization
and product integration evidence remain distinct from installed Telegram E2E.
The owned file-tool branch also runs through the real `run_turn`, file-tool
argument builder, permission wrapper, passive capability check and owned subprocess
for editor, developer and workstation profiles, maximal initial replay, command
lifecycle and child receipt. A separate test passes streamed stdin through the
real descriptor-pinned namespace and permission wrapper. The full `run_turn`
test substitutes the namespace boundary; these tests do not prove productive
Bash/permission-host acceptance or installed Telegram behavior.

Activation needs an explicit deployment task, schema55-compatible candidate and
rollback, exact-source gates and bounded native/Telegram tests for a correction
during a real tool, a late correction, stop and saved-result recovery. The new
gate defaults false; ordinary deployments preserve their existing FIFO.

## Closure

Open until exact-source review and publication. Installed acceptance stays open
after source closure. Inspect tracked/staged/untracked lane state before cleanup.
