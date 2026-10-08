# ADR 0059: Claude process observations and quiet notices

Status: accepted implementation choice; publication validation in progress
Date: 2026-10-08

## Context and decision

Claude's prepared session UUID and stream message UUIDs do not establish a native
turn acceptance or commentary phase. The owning contract is the Claude subset of
[REQ-QUEUE-012](../product/PERSISTENCE_AND_RECOVERY.md#implemented-queue-compatibility-and-local-provider-worker-isolation).
Keep its observations separate from Codex accepted-turn activity and progress
delivery. Reuse the existing SQLite journal, control outbox and passive sender;
introduce no polling service, provider query or new approval path.

The adapter exposes one optional callback after successful owned `Popen`, inside
the registered-process cleanup scope. In file-tool mode this observes the owned
namespace wrapper, not acceptance by Claude. Buffered injected runners and CLI
preflight do not call it. Worker observation lifetime lives in
`worker_claude_activity`; the adapter remains process/pipe cleanup owner.

`ClaudeActivityState` owns each immediate transaction through the existing state
transaction boundary. Schema 42 stores one immutable execution binding per job,
process observation time, irreversible retirement, durable visible cursor,
bounded permission fingerprint, quiet interval and notice episode. Domain SQL
lives outside worker/sender orchestration. SQLite-only binding helpers are shared
by evaluation and first-send validation; lower layers import no runtime facade.

The sender reads committed completed visible messages from the mandatory journal
instead of installing another visible-message callback. This avoids a lost
commit-to-callback interval. Permission evidence uses the existing session mode,
exact active launch and at most 128 payload-free request rows. Its digest excludes
the evaluation clock; pending expiry changes waiting state once. A resolved
roundtrip still changes the digest before first send. Permission launch/binding
loss retires observation rather than interpreting missing evidence as an idle
human boundary. Changed execution bindings suppress evaluation and first send.

No-progress notice creation, episode update and supersession share one transaction.
First unattempted delivery checks live binding, visible cursor and permission
fingerprint in its existing send transaction. Retirement and new activity affect
only never-attempted notices. Attempted/unknown delivery and proven-rejection
retry remain owned by `TaskLifecycleState`; copy is immutable. Optional observer
errors emit bounded closed-site diagnostics and cannot replace strict journal
errors, block result persistence or interrupt owned cleanup/final delivery.
Evaluation deliberately rolls back its bounded batch on an observation error,
including notice creation and episode changes, and emits a bounded diagnostic.
It does not manufacture progress or retire a row whose evidence could not be
evaluated; later cycles may retry observation. Final delivery remains independent.

## Migration, evidence and remaining scope

Integration owner: Hub maintainer. Released schema is append-only. The existing
migration owner retains consistent backup and atomic DDL rollback; schema 41 to
42 adds only the observation table and changes no existing records. Candidate
and runtime rollback must both explicitly support schema 42.

Offline tests use actual fictional processes and temporary SQLite state to cover
worker propagation, process/pipe cleanup after observer faults, journal/send
races, permission expiry/revocation/roundtrip/bounds, stop and binding fencing,
restart episodes, stale retirement and attempted/unknown/rejected sends. Injected
DDL failure and backup tests preserve prior queue and delivery evidence. Source
architecture and independent opposite-provider review, canonical publication and
exact-head hosted checks are separate gates.

This is an ordinary quiet-observation slice. Tool/build lifecycle, live native
acceptance, Claude progress message delivery, exact local transfer, saved-session
connection, durable lead/advisor workflow and subscription-route acceptance
remain open. No installed release or service is changed by this ADR.
