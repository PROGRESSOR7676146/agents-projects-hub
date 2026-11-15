# Codex server cold-boot recovery: candidate design

Status: investigation; not authorized for deployment or implemented.

An interrupted boot can leave executing and queued provider jobs in durable
state. The independent server starts before workers, rejects these jobs, and
consumes its only boot attempt. Workers subsequently recover stale execution,
but queued jobs still block a fresh server start. Replaying or silently
cancelling jobs is not recovery.

An isolated regression also recovers the interrupted execution *before* the
first server attempt. The surviving queued job still blocks that attempt, and
the attempt still consumes the boot marker. Reordering only the existing stale
job recovery is therefore insufficient; it does not address the queued tail.
Both regression cases preserve the queued record and do not invoke a model.
Another isolated case shows that a successful idle-state query does not itself
prevent a later enqueue and worker lease. This demonstrates the query's scope,
not that an unmanaged worker actually raced the installed boot ordering. Any
exception that depends on closed admission needs an independent fence rather
than treating that snapshot as one.

An isolated SQLite `BEGIN IMMEDIATE` experiment blocks a subsequent worker
lease, retains the queued record, and allows that lease after rollback. This is
only a database-write primitive: it does not fence a lease already acquired,
an untracked native launcher or another state store. Nor is a long-held lock an
implemented boot protocol or evidence that existing workers tolerate it. A
crash releases this lock, so it cannot by itself provide a durable closed fence.

## Native daemon alternative

The pinned executable's reported version is 0.166.0; it offers an idempotent
`app-server daemon start` command. The [upstream documentation for that exact
version](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-daemon/README.md)
describes the daemon as experimental, using a separately selected managed
package and optionally running an updater that restarts the server. A pinned
package and disabled updater can avoid the documented automatic update path,
but help text and the upstream description do not prove integration with the
Hub's manifest socket, pin verification, writer guard, service ordering or
rollback. It also does not by itself settle interrupted Hub jobs or establish
the admission fence. No daemon lifecycle command was run against the deployment.

## Candidate boundary

The owning server may start before provider workers only when an exclusive,
boot-scoped admission fence has been durably established and verified. The
fence must stop every path that can claim a provider job or start a native
writer, including external workers and local transfer, throughout server
preparation, listener verification, and handoff. A process scan or a SQLite
snapshot alone is not such a fence. Unknown or unsupported writers, missing
evidence, or an interrupted fence transition must refuse the start.

Under that fence, pre-existing queued jobs remain unchanged and cannot run.
Executing jobs from an earlier boot remain unconfirmed: retain their input,
checkpoints, controls and root exclusion, and never infer completion or replay.
The ordinary exact-result reconciliation must remain available after startup.
No wall-clock timestamp, expired lease, PID absence, or arbitrary database
rewrite alone proves that a previous native execution is finished.

One candidate is to bind each execution lease to the kernel boot identity in
the same transaction that marks the job executing. A missing, malformed or
current-boot identity refuses the exceptional start. This would require an
additive schema migration, compatibility checks for existing jobs and older
rollback runtimes, and a test that an execution cannot be marked started
without its boot identity. A boot identity is only one part of the proof: it
does not close admission or prove that a control sender is quiescent.

Native client and listener ownership, artifact pins, approvals and local writer
checks remain mandatory. The fence is released only after exact server custody
and worker readiness are verified. A crash, stale marker, failed server start or
uncertain release leaves it closed and visible for deterministic recovery. It
must be possible to recover without restoring a database backup over newer
state. Work already held for an owner decision stays held; no new receipt may
fabricate that decision.

This candidate changes the current unconditional refusal of queued and executing
jobs in REQ-OPS-005 and ADR 0088. It is a change to a recovery safety boundary,
not an installed fix. Define precise proof of old-boot ownership and cover
unmanaged writers before implementation. If the existing state cannot prove
that boundary, refuse rather than guess. A supported bounded administrative
recovery remains the fallback when the fence cannot be established.

## Evidence before publication or rollout

- Reproduce the interrupted-boot deadlock on fictional state, including both
  the executing job and queued tail, and verify no old input is replayed.
- Exercise enqueues, worker claims, local takeover and native client races at
  every fence transition, including a stopped or unresponsive writer.
- Inject faults before and after durable fence creation, listener creation,
  worker readiness, fence release and process loss. Verify fail-closed recovery,
  unchanged receipts and root exclusion, and compatible runtime rollback.
- Verify all configured service units, immutable artifacts and client custody
  under the actual boot ordering. Native state and live Telegram acceptance are
  separate from fictional tests.
- Obtain the applicable independent review and canonical/hosted gates. Any
  revised safety boundary requires its explicit owner decision before activation.
