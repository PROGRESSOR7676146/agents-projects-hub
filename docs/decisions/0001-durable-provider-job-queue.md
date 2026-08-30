# ADR 0001: Durable provider job queue and isolated workers

Status: accepted for planned implementation  
Date: 2026-08-30

## Context

The current central Hub ingress deterministically selects a provider, but the
same request path can wait for provider execution. A slow, exhausted, or failed
provider must never make local commands or another provider unavailable. The
product also needs a recoverable record of a Telegram request after it has been
accepted, without treating an interrupted provider turn as safe to run again.

This record defines a replacement execution architecture. It is a contract for
the planned work; it does not claim that the queue or workers are implemented.

## Decision

The product will use a deterministic **Hub Controller**, a durable SQLite job
queue, isolated provider workers, and a separate **Hub Telegram bot** as the
central Telegram identity.

```text
Hub Telegram bot -> Hub Controller -> SQLite provider job queue -> provider worker
                         |                    |                       |
                         +-> local commands   +-> Telegram outbox      +-> result

Hermes Gateway and Agent Session Remote/tlive remain independent recovery and
session-control channels. They are not required for ordinary queue progress.
```

The Hub bot owns project-group ingress, command menus, and callback handling.
It has Telegram Privacy Mode disabled as a deployment prerequisite so the
Controller can receive ordinary admitted group messages. Its token lives only in
a restrictive local private file and is never committed, logged, or included in
examples. Provider bots do not poll project groups; they retain their own
identities for responses and provider-specific direct messages.

The Controller is not an LLM agent and MUST NOT call a provider CLI, provider
RPC, or app-server in its Telegram update path. It may use deterministic local
state to validate admission, route a message, enqueue a job, read cached health,
and answer local commands. A worker is the only component that starts or resumes
its provider's productive turn.

There is one worker runtime per provider identity. Each worker takes only jobs
for its own `agent_id`, owns provider-specific session lifecycle and structured
error classification, and writes bounded results back to SQLite. It does not
poll project Telegram groups. A provider worker failure therefore degrades that
provider only.

### Topic ordering and snapshots

Productive work uses strict FIFO per numeric topic. At most one provider job in
a topic may execute at a time, regardless of target agent. Different topics may
run concurrently. This deliberately preserves a comprehensible visible journal,
provider-session writer ownership, agent handoff ordering, and command
semantics. Parallel lanes are deferred until a separate decision supplies an
equally clear ordering and writer model.

At enqueue time the Controller atomically snapshots the selected target agent,
provider session reference and generation, model/effort selection, and bounded
visible-context reference. Later changes to active agent, model, or effort do
not rewrite an existing job. Reply and explicit-mention routing retain their
existing deterministic precedence.

### Durable acceptance and idempotency

The Controller accepts an admitted Telegram update in one short SQLite
transaction. That transaction MUST record the idempotency receipt and visible
message, resolve the immutable project and numeric topic, assign the next topic
sequence, capture the execution snapshot, and insert one queued job. Only after
commit may ingress advance its Telegram offset. Network calls and provider work
MUST NOT occur inside this transaction.

The job's idempotency key is unique. A delivered duplicate update returns the
existing record and MUST NOT create another provider turn. Persisted state and
state files remain local and restrictive in permission.

### Job state machine

The planned durable job states are:

```text
queued -> leased -> executing -> result_ready -> completed
                 \-> retry_wait -> queued
                 \-> failed | cancelled | indeterminate
```

- `queued`: accepted durable work awaiting the eligible worker.
- `leased`: a worker atomically reserved work using a random lease token, but
  has not started the provider invocation.
- `executing`: the worker has recorded that provider invocation can have begun.
- `retry_wait`: a retry is scheduled after a failure conclusively shown to have
  happened before provider execution.
- `result_ready`: the worker has committed the bounded result and an outbox
  message, but Telegram delivery is unfinished.
- `completed`: the outbox has recorded successful delivery.
- `failed`: an understood terminal failure occurred.
- `cancelled`: the owner cancelled work before provider invocation.
- `indeterminate`: the worker was lost after execution might have begun and
  reconciliation cannot establish a safe outcome.

Workers lease the oldest executable job for a topic and provider atomically.
They heartbeat the lease and may complete a job only with its current token.
An expired `leased` job may return to `queued`. An expired `executing` job MUST
NOT be blindly rerun.

### Interrupted execution, retries, and reconciliation

The system distinguishes four classes of failure: transient-before-execution,
quota, authentication/permanent failure, and ambiguous execution. Automatic
retry is permitted only when the worker can prove the provider turn was not
accepted or started. It uses bounded backoff and a bounded attempt count.

After a worker loss or timeout in `executing`, the provider-specific worker may
reconcile a known session/result using structured provider capability. If it
proves no turn began, it may return the job to `queued`; if it proves the result,
it may commit it normally. Otherwise it MUST mark the job `indeterminate`, stop
automatic execution, retain safe diagnostic metadata, and notify the owner with
a bounded recovery choice. It MUST NOT automatically repeat file edits, commits,
or other potentially external effects.

Quota handling is account-scoped first. A worker may mark a known account
limited, rotate only through the existing approved account mechanism, and
verify the selected account. The job is retried only under the same
pre-execution proof rule. When all accounts are unavailable, the provider's
circuit is open while the Controller and other workers remain usable. Account
labels in results and alerts are masked.

### Result commit and Telegram outbox

On a successful provider turn, a worker commits, in one transaction: the
bounded visible result; session metadata; applicable context acknowledgement
watermark; any handoff acknowledgement; a Telegram outbox row; and the job's
transition to `result_ready`. Context is never acknowledged merely because a
job was leased or began executing.

An outbox sender, separate from provider execution, delivers the prepared
message and advances `result_ready` to `completed`. Telegram failure retries
only outbox delivery; it MUST NOT invoke the provider again. If Telegram
accepts a request but the sender dies before persisting its message identifier,
an occasional duplicate publication is possible. The system must prefer that
bounded transport duplicate over repeating a productive provider turn.

### Commands and writer ownership

Local controller commands are outside the provider job queue and use only local
state or cached health. In particular, `/menu`, `/status`, `/accounts`, and
`/model` remain responsive while any worker is slow or unavailable. `/new`,
`/local`, and `/return` must reject or explicitly coordinate with queued,
leased, or executing work; they must not create a second writer or mutate an
execution snapshot. The existing provider-session writer lease remains
authoritative.

### Rollout and rollback

Implementation is additive and gated per provider:

1. Provision the separate Hub bot through private local deployment state,
   disable its Privacy Mode, and verify that provider bots no longer poll
   project groups. This is a deployment acceptance step, not repository
   configuration.
2. Add versioned SQLite schema and repository APIs, with the existing
   SQLite-consistent backup and migration rollback guarantees. Runtime behavior
   is unchanged.
3. Add durable enqueue and a compatible embedded consumer behind a feature
   flag, proving ingress does not wait for provider work.
4. Deploy the isolated Codex worker and outbox, then enable queued execution for
   Codex only after health and fault tests.
5. Enable OpenCode and Antigravity independently after each adapter meets the
   same contract.
6. Retire the synchronous path only after live acceptance. Retain a temporary
   compatible fallback flag for one release cycle; archive, rather than delete,
   legacy dispatch records until the rollback window closes.

A rollback stops new workers and disables the per-provider queue flag without
dropping the additive schema or destroying queued, failed, or indeterminate
records. A prior binary must not be started against an unknown schema version.
Migration backup restoration is reserved for a failed migration, not ordinary
feature rollback.

## Consequences

- A provider outage, quota exhaustion, or slow turn cannot block controller
  commands or an unrelated provider/topic.
- A queued request survives Controller restart after commit; a prepared result
  survives Telegram delivery failure.
- Exact recovery of an in-flight provider turn remains intentionally impossible
  without provider-specific proof. `indeterminate` is a visible safe state, not
  an error to hide with retry.
- SQLite schema, repository APIs, worker protocol, outbox, monitoring, and
  tests are new implementation work. The Controller must no longer import or
  directly own provider execution after the rollout completes.

## Required acceptance evidence before removing the legacy path

Automated and owner-driven live checks must demonstrate all of the following:

1. Controller commands answer without waiting for a stopped or slow provider.
2. A stopped worker for one provider does not prevent another provider from
   completing work in another eligible topic.
3. Repeated delivery of one Telegram update produces one durable job and at
   most one productive provider invocation.
4. Controller restart preserves committed queued work and strict topic FIFO.
5. Worker loss before `executing` can safely recover its lease; worker loss
   after `executing` does not automatically rerun the turn and becomes
   reconciled or `indeterminate`.
6. Telegram delivery loss retries only the outbox message, not provider work.
7. Reply/mention routing, context acknowledgement, session generation, and
   writer-lease protections continue to hold under queued execution.
8. Exhausting one provider/account leaves the Controller and other providers
   available, emits only masked bounded operational information, and does not
   weaken approval or sandbox policy.
9. The Hub bot alone receives ordinary project-group ingress, serves the group
   menu and callbacks, and has Privacy Mode disabled; provider bots retain
   response/DM identity without group polling.
10. Migration backup, feature-flag rollback, privacy scan, and all relevant
   migration/concurrency/fault-injection tests pass before live cutover.

## Alternatives rejected

- A separate LLM router: routing and scheduling are deterministic and must
  remain available without any model.
- One synchronous Controller with background threads but no durable queue:
  process loss and provider blocking remain coupled to ingress.
- Automatic retry after an unknown in-flight turn: it can duplicate edits or
  external effects.
- One global queue: it unnecessarily serializes unrelated topics and providers.
- Parallel execution within a topic in the first version: it weakens session and
  visible-context ordering without a demonstrated need.
