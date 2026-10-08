# Queue and process recovery

Status: active runbook  
Last updated: 2026-09-13

This runbook covers the durable Controller, provider-worker, and Telegram-outbox
topology. It contains reusable procedures only; deployment identities, paths,
chat IDs, account hints, and live evidence stay in private operator records.

## Safety rules

- Back up the SQLite database before migration or manual investigation.
- Repair or restart only the failed component. Controller, workers, sender,
  Hermes, and tlive deliberately have no mandatory service dependency chain.
- Do not edit queue tables by hand, copy a live SQLite file, or turn an
  `executing`/`indeterminate` job back into `queued` merely to make it move.
- Do not remove a Codex Unix socket until local process and lock inspection
  proves that no live owner can exist.
- Never paste configuration, tokens, provider output, or database rows into
  Telegram or a public issue.

## Read-only triage

```bash
agents-projects-hub doctor HUB_CONFIG
agents-projects-hub status HUB_CONFIG
agents-projects-hub monitor HUB_CONFIG
agents-projects-hub indeterminate-audit HUB_CONFIG
systemctl --user status agents-projects-hub.service
systemctl --user status agents-projects-hub-sender.service
systemctl --user status 'agents-projects-hub-worker@*.service'
```

These commands never create or migrate state. `doctor` and `status` only read
it and refuse a missing database or a non-current schema; `monitor` refuses
the same and records only its own health, alert and repair bookkeeping.

Resolving an indeterminate job is a deliberate state change, not triage: it
records the operator's decision and lifts the root hold, so later work on that
root may start. Run it only after the audit and a private review of the job:

```bash
agents-projects-hub indeterminate-resolve HUB_CONFIG JOB_ID --resolution acknowledged
```

Interpret the components independently:

- Controller down: new Telegram ingress and local commands stop; committed
  queue work and independent recovery channels remain.
- One worker down: only that provider stops taking new jobs; other workers with
  eligible independent execution scopes and Controller commands remain
  available.
- Capacity is cache-only in `status.execution_capacity`: `occupied` names only
  bounded worker/agent/phase owners, while `blocked_uncertain_scopes` is an
  aggregate and never reveals roots or topics.
- Sender down: completed provider results remain `result_ready`; workers MUST
  NOT repeat provider execution to compensate for missing Telegram delivery.
- Hermes or tlive down: the other channels remain independent; no timeout is an
  approval.


## Unknown final or progress delivery

In schema43, inspect `unknown_delivery` and `unknown_progress_delivery` separately
from pending retry counts. The corresponding passive alerts mean an attempted
Telegram send lacks a trusted committed receipt; restarting the sender does not
authorize resend. Use the [exact-job diagnostic](OUTCOME_JOURNAL.md) to inspect
delivery status, part receipts and provenance locally. Preserve the saved result,
earlier receipts and artifact spool. Never retry provider execution to repair
delivery, replace an unknown notice or hand-edit a delivery status.

An unknown final/notice outbox blocks later deliveries in the same numeric topic.
When its job is result_ready, later productive jobs in that topic remain blocked
too. The schema44 local-owner control is described below; activation still
requires independent exact-candidate review of it and the delivery prerequisite.
Source publication is insufficient. Restart, retry exhaustion or age cannot
resolve unknown. Before an authorized activation, check foreign keys and orphan
parts on a consistent disposable copy, then run the candidate migration gate.

A late exact Codex terminal observation may record terminal proof and completion
text while retaining unknown/attempted notice delivery and the indeterminate job.
Any sending notice first defers observation until the sender settles it. A strict
receipt or proven rejection then permits normal reconciliation, which archives
every original part and its receipt/artifact metadata before replacement.
`notice_delivery` is separate from `result_delivery`; the former cannot prove
that the recovered final result reached Telegram. No new artifact snapshots are
claimed as retained in that evidence-only branch. Reconciliation requires a
separately reviewed explicit action. Migrated legacy sends stay unknown and
legacy receipt provenance stays unverified. A runtime rollback must support
the activated schema; an older binary is not a compatible sender recovery.

### Local owner release of a delivery hold

On a reviewed schema44 release, preview one exact outbox locally:

Find its opaque ID from the existing exact-job outcome diagnostic. If the job
ID is unavailable, this bounded local read-only query lists only unknown target
identities, never prompt/result text or credentials:

```bash
sqlite3 'file:/home/example/private/state.db?mode=ro' \
  "SELECT outbox_id,job_id FROM telegram_outbox WHERE status='unknown' ORDER BY created_at,outbox_id LIMIT 100;"
```

```bash
agents-projects-hub delivery-hold /home/example/private/state.db EXAMPLE_OUTBOX_ID
```

The preview opens existing state read-only. Inspect its saved-final/notice target,
part and receipt counts, numeric destination, current hold status and snapshot.
Inspect `control_consequences` before consenting. The topic binding is retained
for the decision's lifetime. For a `result_ready` target, `/new`, model/agent
changes and `/local`/`/return` remain blocked without a time limit; local/terminal
transfer across the same scope and drain to `managed_externally` also remain
blocked. This action only permits queue continuation. A future control
reconciliation requires a separate reviewed decision owned by the Hub maintainer.
First apply requires an established canonical scope. Legacy/empty scopes refuse;
use the existing trusted runtime reconciliation first, never direct DB edits.
If the owner chooses to let already authorized queued work continue without
confirmed delivery, apply with the exact preview token:

```bash
agents-projects-hub delivery-hold /home/example/private/state.db EXAMPLE_OUTBOX_ID \
  --apply --snapshot EXAMPLE_PREVIEW_SHA256 \
  --continue-without-confirmed-delivery
```

For the same uncertain-commit retry, retain the original command/token; preview
also exposes `disposition_snapshot` separately from the fresh target snapshot.
`disposition_binding_changed` means a historical decision exists but no longer
matches the target; it cannot release a different binding or be replaced with a
fresh decision. A stale first apply requires another preview.
An exact apply retry reports the recorded decision and its current `hold_status`;
it must not be read as a new permission after out-of-band binding damage.

Check `unknown_delivery`, `outstanding_delivery_holds` and `released_delivery_holds`
plus the [exact-job diagnostic](OUTCOME_JOURNAL.md). Its delivery remains unknown,
with unchanged parts, provenance and artifacts. Another unreleased hold can still
block the topic. The command changes no execution, held-request decision, stop,
writer, session or artifact state. Apply may allow authorized tail jobs to start
immediately under ordinary scheduling; it never resends the old message or calls
the provider itself. Existing session/writer, connect/adoption and relocation
checks remain independent. See [REQ-QUEUE-005](../product/PERSISTENCE_AND_RECOVERY.md).

## Component restart boundaries

After inspecting the failed unit and preserving its logs privately, restart
only that unit:

```bash
systemctl --user restart agents-projects-hub.service
systemctl --user restart agents-projects-hub-worker@AGENT.service
systemctl --user restart agents-projects-hub-sender.service
```

`SIGTERM` is cooperative. Polling and transport waits are bounded. A lease
observed after stop but before invocation is returned without consuming an
attempt. A signal can race after the final safe boundary; work past that point
uses the conservative rules below.

## Provider-job recovery

### Codex preparation notification overflow

An older client may report `Codex notification buffer exceeded its bound` while
initializing or preparing a thread. Distinguish this local preparation failure
from a provider quota rejection using the durable job classification and exact
checkpoint, rather than assuming every failed Reply reached the model.
Confirm a caught preparation failure before `turn/start` and that its failure
notice was delivered. A missing accepted-turn ID alone does not prove that
invocation never happened. Preserve the original attempt and its partial effects;
never rewrite its status or automatically resend the input.
The same error after `turn/start` was sent, including while awaiting its
acknowledgement, means an uncertain turn: use exact Codex turn recovery rather
than preparation-failure handling.

The prepared client filters foreign notifications before its bounded useful-result
queue. It handles approvals, their resolutions and account quota updates separately,
and preserves exact-current-turn final items and context updates, including those
received before the `turn/start` acknowledgement. An active owning connection must
still receive its final events. The synthetic two-worker regression demonstrates
this conservation; it does not prove the source or fan-out of native broadcasts.
Useful-result and optional-activity limits remain fail-closed guards.

Before an owner-authorized temporary worker restart, take a SQLite-consistent
backup and verify that the exact affected worker has no active or uncertain
provider turn. Restart only that free slot; preserve other productive workers,
the shared daemon, sender and approvals host. A cached idle worker label alone
does not prove provider terminality. Only after a proven pre-`turn/start`
preparation failure may one explicit continuation follow after
rechecking the attempt, current queue, session binding and delivered notice;
reconcile an unknown prior outcome before considering another invocation.

Before calling the root cause closed, separately verify the exact installed
revision and every required component, then run the coordinated independent-root
scenario in the [stabilization backlog](STABILIZATION_PLAN.md#live-acceptance-backlog).
Use only bounded method/count and binding metadata for private protocol attribution;
do not expose reasoning, prompts, tool payloads or raw protocol streams. Neither
quota status nor a restart alone proves the buffering fix is installed.

### WebSocket transport backpressure

The native Unix WebSocket inbox retains raw frames in FIFO order, with limits
of 1,024 queued frames, 8 MiB serialized input bytes and 4 MiB per frame.
JSON parsing happens on consumption. One received frame may wait outside the
inbox for capacity; parser, string and WebSocket-library overhead remain separate
from the serialized-byte budget. Outbound messages are immutable serialized
snapshots, limited to 16 queued frames and 4 MiB each.

Ordinary inbound saturation pauses only its asynchronous producer. Sending,
receive deadlines and close remain independent. Accepted frames drain before
the recorded terminal error; terminal state needs no queue slot. No protocol
frame is coalesced or classified by the transport. Client-level filtering still
selects the current turn's results.

A disconnect or explicit close can interrupt a frame still waiting for
capacity, and an upstream slow-consumer policy can end the stream. Apply the
ordinary exact-turn recovery rules below; backpressure never proves completion
or authorizes replay.

The official stdio transport uses the same inbound wire-byte/frame budgets,
with a blocking pipe reader instead of an asynchronous WebSocket receiver.
Bounded JSONL reads reject oversized lines, including unterminated lines, before unlimited
allocation; valid final JSON without a newline remains supported. UTF-8
admission also checks multibyte text. One decoding frame, Python objects,
client-retained events and OS pipe buffers are outside the queued wire-byte
budget. This is not a total process-memory limit.

Stdio uses a dedicated writer and a nonblocking, immutable outbound queue with
16 slots and an 8 MiB aggregate UTF-8 payload budget; each frame is at most
4 MiB. A full outbound queue is refused visibly, never silently dropped. This
allows a request larger than the pipe capacity to progress while the caller
drains notifications, instead of forming a bidirectional pipe deadlock.
Successful `send()` means queue admission; the exact RPC response remains
submission evidence. EOF or a read failure seals both queues. A physical writer
failure seals only outbound: stdout may still contain final, approval and telemetry
frames. The reader continues, applying the same backpressure. One transport lock
selects the first recorded cause; both queues use that cause when they terminate.

Stdio close wakes blocked producers/consumers before process cleanup. Accepted
frames drain before the first EOF/read/size error; a receive timeout leaves
later delivery usable. Shutdown is checked before each outbound write; a write
already past that final check may have been submitted and needs ordinary uncertainty
handling. Client-retained event bytes and a shared preparation budget remain
separate resource-lifecycle work; do not interpret receipt traffic as productive
progress or approval.

For headless stdio, Hub attempts only explicit declines or unsupported-request
errors. A typed refusal to admit such a reply after EOF/read/write termination
does not discard later buffered output. Other send errors still fail normally;
explicit close is a cancellation boundary. A writer failure can occur after
successful admission, so the client also observes physical write faults while
consuming frames and immediately before its completion checkpoint. It never
claims that an admitted decline was delivered.

After detecting a failed response channel, Hub reads for at most **20 seconds**,
with one fixed deadline. A quiet stdio receive checks for faults on its next
poll, normally within one second, without renewing its original quiet timeout.
Notifications, approvals and queued frames cannot renew the drain window. These
are consumption bounds, not a hard real-time guarantee through synchronous
callbacks or local I/O, nor a promise to save an arbitrarily late pipe tail.

Completion after this fault requires explicit matching thread and previously
accepted turn identity with native status `completed`. Hub appends a fixed,
payload-free transport notice before persisting the completed result; raw
visible-item callbacks retain provider text. The same notice remains in bounded
partial failures without replacing permission, provider or storage failure
causes. Missing proof, EOF or expiry retains ordinary uncertainty and root
exclusion, with no resubmission or permission grant. An ordinary EOF without a
refused reply or observed physical write failure adds no warning. If completion
is persisted before any writer fault is observable, a later fault cannot amend
that checkpoint; queue admission provides no per-frame delivery receipt.

Explicit close wakes both queues immediately and may discard frames that never
entered inbound. Repeated exception traceback retention and descendant-held
pipes remain separate cleanup/resource work; this slice neither adds process-group
authority nor establishes a durable owned-process exclusion barrier.

### Codex RPC response deadlines

Each RPC without an explicit caller deadline has a fixed 120-second response
budget, computed before send, and a 20-second quiet receive ceiling. Explicit
metadata/control deadlines keep their existing budget and receive allowance.
`turn/start` uses a fixed 300-second response deadline, allowing a quiet early
human approval wait within that window. Notifications and approvals never renew
or suspend either deadline. A frame arriving at or after expiry is rejected
before interpreting a result, rejection or approval. Optional post-completion
quota reads retain their five-second total budget.

These are local response-consumption policies, not native protocol guarantees,
a productive-turn duration or one whole preparation budget. Separate RPCs,
connection startup, synchronous parsing/callbacks and local I/O remain separate;
the legacy Unix JSONL transport does not enforce receive timeouts.

A timeout after sending `turn/start` may hide native acceptance. External and
embedded queues retain `indeterminate`, the prepared thread checkpoint and root
exclusion when no exact accepted turn ID was saved. They never infer that no
turn started from the missing ID or retry the submission automatically.
Only the typed local RPC response-budget error, caught by the exact
pre-submission preparation boundary, may create the existing saved-task retry
ticket. The owner must Reply exactly `retry` to its delivered failure notice;
the ticket retains the original payload and binding and grants no tool permission.
Generic errors with identical wording, permission-profile refusals, unwrapped
deadline errors and contradictory execution evidence create no such authority.
This type does not attest an upstream timeout: the local response budget also
includes the bounded response-channel fault drain. Missing saved context after
thread replacement still refuses retry. No timeout creates automatic replay.
Legacy inline execution retains its weaker dispatch/recovery boundary.

### Completed Codex socket connection retirement

A productive WebSocket client is disposable after its exact accepted thread and
turn report explicit `completed` status and the result publication succeeds.
External and embedded workers first settle controls and persist the result/outbox;
the legacy inline route first sends the result and records its successful dispatch.
Inline delivery retains its existing non-atomic recovery limitations.
The single-use proof cannot come from another turn, missing/unknown status,
failed publication or a covering stop. New preparation invalidates old proof.

Retirement detaches only the expected cached client, then closes its connection;
it sends no unsubscribe, interrupt, archive or delete RPC and never stops the
shared daemon. A close exception records the bounded
`completed_socket_retirement_error` warning without undoing the saved result.
The local transport seals its inbox and gives its receiver up to five seconds
to join. A bounded return alone confirms neither receiver termination nor
immediate server-side subscription teardown, and a join timeout need not warn.
Optional quota collection has one five-second total deadline and can use the
just-completed turn's rolling windows once.

The next invocation opens a fresh connection and resumes the saved native thread.
The existing unpinned socket-to-stdio fallback exception still applies
([REQ-AUTH-004](../product/ACCOUNTS_CONTROL_AND_SECURITY.md)); reconnect does not
promise exact continuity when that fallback replaces a legacy thread. Retiring
after each successful turn causes more connection attempts, increasing exposure
to that configured fallback on a transient construction/initialize failure.
External workers reconsider fallback at idle through a bounded metadata handshake;
legacy inline/embedded services retain their preexisting fallback mode until
service recovery. This slice does not change their fallback policy. Stdio
retirement is deliberately disabled: disposal of its owned process requires a
separate durable lifecycle barrier across workers/restarts before root exclusion
may be released. A process-local poison flag would not provide that barrier.

Queued native-discovery menus, including stale and explicit refresh callbacks,
read only the catalog cache. The independent monitor owns metadata refresh through its own
connection; Controller must not become a competing reader of the productive
client. See the [offline native evidence and live boundary](../testing/README.md#optional-offline-native-notification-attribution).

### Codex live-control contention

The accepted-turn stop/steer observer opens only existing current-schema state
with a short lock timeout. SQLite BUSY/LOCKED, including extended result codes,
retain the exact pending state operation for the next stop-first poll; they never
repeat a provider call or lease another follow-up while settlement is pending.
Shutdown makes one final bounded state-only attempt and releases a pending
unstarted lease. A child proven unsent or explicitly rejected may return to the
queue through its original token. Persistent contention, expired tokens or
uncertain invocation retain the conservative durable disposition and require
the recovery rules below. Do not requeue a child because the parent result was
recovered.

An explicit steer rejection disables steering for that parent while stop polling
continues. The follow-up remains FIFO work for normal execution after the parent
completion/delivery boundary. A rejected settlement that cannot commit retains
its conservative execution disposition and does not authorize another RPC.

A permanent steering-path failure disables steering, retains the first error
and continues stop polling. The worker evaluates an observed stop before late
lookup or that deferred error. Opening/stop-lookup failure and unconfirmed
shutdown enter the ordinary failure path. Saved parent completion may still be
recovered; a stopped observer checks shutdown again before any new RPC, while
an executing child keeps its root blocker. An interrupt is attempted once, and
its acknowledgement is not proof that the provider turn ended. Read the exact
accepted turn before releasing uncertain work or coordinating a restart.

Bounded private diagnostics distinguish contention, monitor failure,
unconfirmed interruption and cleanup failure without recording exception text.
Control RPCs have total response deadlines, including notification traffic.
Shutdown closes an active control client before joining the observer; the
client close allowance and the ten-second join allowance are additive. If native
waiting or a visible callback also failed, that original error remains the
primary failure and the cleanup problem is recorded separately.

### Durable dispositions

Repeated Codex notification-buffer failures before `turn/start` require a
transport/consumption investigation even after an idle-worker restart. Keep
the exact failure notices and saved inputs. Inspect subscriptions retained by
successful clients and notification traffic during initialize, preparation and
submission; a demonstrated retained subscription is not exact attribution of
every deployed overflow. Restart is temporary recovery, not closure evidence.
Do not restart an active worker or the shared daemon to clear this condition.
Any maintenance restart needs a separately approved drained boundary and a
fresh consistent backup.

For a delivered text-only preparation notice offering saved-task retry, the
owner can Reply exactly `retry`. Schema 41 records one child while keeping the
failed source and original input membership. A second preparation failure needs
an explicit Reply to its own new notice; repeating the older notice cannot
create another child. Inspect the child payload/context and exact session
binding before calling the task recovered. Materials, missing legacy tickets,
changed bindings and contradictory execution evidence cause refusal. Follow
[REQ-QUEUE-004](../product/PERSISTENCE_AND_RECOVERY.md) for the safety contract;
never grant or replay native tool permissions from task authorization text.

This is a new owner input in FIFO arrival order. Later work already queued or
completed can precede it in the same session; retry does not restore the old
queue position or rewind native context. Configured Codex aliases keep their
actual agent identity. Runtime provenance comes from trusted local configuration,
not the agent's label. Reconfiguring that alias to another runtime must refuse
the saved retry before either worker dispatch or material preparation; its
durable notice must say the provider did not start and must offer no new ticket.
A changed child snapshot or attached child material refuses
before preparation. An unavailable root refuses retry authority while preserving
the original failure and notice.

The ticket update guard is installed by the unreleased schema-41 migration.
An existing development database already marked schema 41 does not rerun that
migration on reopen; discard only disposable fixtures or prepare an explicit
upgrade before retaining such a database. Production migration and deployment
remain separately authorized.

Preparation that replaces an existing Codex thread cannot offer saved-task
retry without a frozen effective-context snapshot. The fallback's bounded
visible-context bridge currently exists only in memory. Hub refuses new tickets
and previously saved tickets and descendants for that transition rather than
submit a shortened task. Missing, cyclic or over-64-generation retry ancestry
also refuses rather than inferring safe context. Send a new request with the
complete task and relevant context. Preparing
the first thread from no prior identity, or retaining the exact existing thread,
remains supported. Effective-input preservation and historical context for an
ordinary Reply need separate follow-up; this conditional failure test does not
establish the deployed overflow's cause or a natural post-binding trigger.

- Expired `leased` means provider invocation was not recorded as possible. The
  scope may be claimed by another eligible job; normal stale recovery returns
  the old job to `queued`, and the expired token cannot start it late.
- Expired `executing` means invocation may have begun. Normal stale recovery
  marks it `indeterminate` unless a provider-specific structured reconciliation
  proves a result or proves that execution never began. Unresolved uncertainty
  retains its canonical-root execution scope across topics and providers, but
  does not consume the global worker-capacity count or block another root.
- `failed` and `cancelled` are terminal. Do not reinterpret them as pending.
- `result_ready` means provider work already succeeded. Only Telegram delivery
  remains; never submit another provider turn for the same job.

If a provider has no safe reconciliation capability, retain the
`indeterminate` record, inspect the project and provider session locally, and
create a new explicit user request only after deciding whether duplicate side
effects are acceptable. Resolve the reviewed exact old job before expecting new
work for the same root to execute; resolution releases only the scope and never
replays the old job.

`indeterminate-audit` classifies all retained uncertain jobs from read-only
SQLite evidence and prints only aggregate counts. To preserve a detailed local
record, pass `--output PRIVATE_PATH`; the command creates a new mode-`0600` JSON
file and refuses to overwrite an earlier report. The report never authorizes
productive replay. Its evidence classes distinguish a persisted result, saved
completion, partial text, accepted turn without visible output, thread creation
without accepted turn, and absence of an execution checkpoint. Schema-34 reports
confirmed failed/interrupted turn status separately from partial evidence and
recommends the specific failure notice for owner continuation. Notification
status is reported separately so an undelivered uncertainty notice is visible.

For an accepted Codex turn with a transport loss, the worker makes bounded
read-only exact `thread/read` observations. Confirmed completion publishes the
stored result without a new turn. Confirmed failed/interrupted status retains
the old job and sends a corrected notice; the owner can Reply `retry` to that
specific notice for a new inspection-first turn in the same session. Earlier
queued requests on the root remain paused and visible. Active or unproven
status keeps root exclusion. Do not use `indeterminate-resolve` merely to make
a failed-turn continuation move, and do not reset the old job to `queued`.

When the owner already opened a native Codex CLI outside Hub ownership, first
check whether it uses the owning app-server through `--remote`. A standalone
`codex resume` is a separate persistence writer. Wait for its current turn to
finish and for the owner to close it at an idle boundary; do not infer this
from missing process IDs or restart the shared app-server. After release,
schema and plugin compatibility checks, preview the exact session with
`agents-projects-hub session reconcile-existing-local` using the private
session, provider thread, old job, generation and canonical root. Apply with
`--confirm-cli-closed` only after the owner assertion, or with
`--confirm-remote-idle` for an already remote idle CLI. Read back the exact
session and old job. A successful apply prints a same-session `--remote`
resume command for reopening a closed standalone CLI. This changes writer
ownership only; the old uncertainty
and any paused queue remain. `/return` later requires the owner to close the
CLI and does not replay the old job. The Telegram owner UI uses the specific
failure notice, with no job ID command required.

After reviewing one exact job, `indeterminate-resolve` can append one fixed
operator classification: `acknowledged` means the uncertainty was reviewed,
`superseded` means a later explicit request made the old outcome irrelevant,
and `externally_completed` means completion was confirmed outside Hub. The
command is idempotent for the same value and rejects replacement. It does not
change the original job or error, send a message, or authorize provider replay.
The audit reports these annotations separately and recommends no further action
for resolved records. Schema 32 uses the immutable annotation to release the
canonical-root scope for unrelated future work.

## Persistent local root blockers

When a local or terminal writer retains a canonical root, new productive
messages in another topic receive a durable Hub refusal and do not enter the
provider queue. Open the linked owner topic, close the CLI at an idle boundary,
then use `/return` there (or `/release` for a managed terminal). The command
only changes the writer lease and retains the provider thread. Already accepted
queued work is held before the lease returns; the owner must use the exact
notice to confirm or cancel that job. A late `/return` alone does not run it.
The sender may retry an undelivered blocker notice without starting a provider
turn. Inspect `/status` in the topic for its owner-facing blocker reason; do
not use PID absence or lease age as proof that the CLI closed. Schema 35 is
additive and requires schema-compatible Hub, workers and sender for rollback.

## Capacity and lane changes

`max_parallel_roots` defaults to 1. Raising it requires external queue mode and
does not create extra processes: actual parallelism is also limited by the
configured provider-worker units. Lowering it never cancels active work. Restart
workers with the smaller configuration; once the first restarted worker polls,
all fresh workers use the lowest advertised value and take no new lease until
occupied execution falls below it. An increase remains conservatively at the
old advertised value until each old worker restarts or its declaration ages out.

For three simultaneous Codex projects, set `max_parallel_roots: 3` and
`codex_worker_count: 3` in the private configuration, with external queue mode,
Codex in `external_worker_agent_ids`, and `manage_codex_server: false`. Install
the versioned Codex slot template and start the existing
`agents-projects-hub-worker@codex.service` plus
`agents-projects-hub-codex-worker@2.service` and
`agents-projects-hub-codex-worker@3.service`. Each process owns a separate
Codex client and SQLite connection. The shared Codex socket service remains a
separate component. Check `status.runtime_health.provider_workers` for all three
slot IDs and require an exact clean revision convergence before calling the
rollout current. Queue, root exclusion, and fairness remain the acceptance
boundaries; a running process alone is not provider E2E evidence.

For an existing installation, copy the new template from the exact clean
candidate revision into the private user unit directory, then reload systemd
before enabling the two numbered units. Do this only as part of an authorized
deployment with a verified rollback artifact and a validated private config.
The bootstrap installer copies the same template for new installations.

Claude uses the same capacity rule with its own `claude_worker_count` and
`agents-projects-hub-claude-worker@2.service` / `@3.service` template slots;
slot one is `agents-projects-hub-worker@claude.service`. With three Codex and
three Claude slots, `max_parallel_roots` remains a single shared limit: set it
to three for at most three productive projects total, or increase it explicitly
only after assessing the combined load. The Claude adapter currently has no
tool access or human approval bridge. Keep its units disabled until private CPA
routing, account/fallback policy, exact revision and live acceptance are checked.
To reduce Claude capacity, drain and stop the highest numbered Claude units
before lowering `claude_worker_count` and restarting the remaining workers.

To reduce the Codex slot count, stop and disable the highest numbered units
first, then lower `codex_worker_count` in the private config and restart the
remaining workers. Wait for active turns in the stopped units to finish before
stopping them; do not force-stop a productive turn to reclaim capacity. A
configuration edit does not reconfigure an already running process.

Create and bind a lane only through the local CLI. Binding and archival refuse
queued, leased, executing, retrying, result-ready, unresolved, dispatch-owned,
local-writer-owned, or provider-bound topics. Start a fresh unbound session
before changing its root; archive first, then clean up. A lane is never
selected from Telegram input, and a worker refuses a path that is not the exact
derived, allowlisted and currently registered Git worktree.
Bound archival also validates the current registered base Git root and checks
that destination scope is idle before committing. An unavailable, disabled or
invalid registration must be corrected locally before retrying archive. See the
[archive correction](../decisions/0031-bounded-root-concurrency.md#archive-correction-2026-10-08).

## Changing provider ownership

Before changing a locally queued provider to `managed_externally`, stop new
admission for that provider and inspect its durable jobs. Drain `queued` and
`retry_wait` work with the old eligible worker, let the sender finish
`result_ready` work, and apply the conservative recovery rules above to any
`leased` or `executing` row. Cancellation is allowed only as an explicit local
operator decision for work that state validation still identifies as safely
unstarted; do not edit SQLite directly.

The Controller deliberately refuses startup when a configured externally
managed agent still owns any nonterminal local queue row. This is a diagnostic
barrier, not an automatic migration: restore the prior locally managed
configuration to drain safe work, or reconcile/cancel it through reviewed state
operations, then validate the new configuration again. Never start a native
gateway and a local worker as competing consumers for the same provider.

## Telegram outbox recovery

- Diagnose the cached sender health before changing queue state. A current
  `transport_operation` plus `transport_failure_class` distinguishes delivery
  timeout/DNS/TLS/I/O from an API rejection; safe status and retry-after may be
  present. The consecutive count describes the current episode and resets only
  after a successful Telegram request. Runtime events are edge-triggered, so
  one recorded error can represent many retries.
- Sender-scoped stale recovery returns an expired unattempted `sending` lease
  to pending. A begun send becomes unknown; no automatic resend follows even
  when Telegram may have accepted it immediately before sender loss.
- Retry exhaustion leaves the outbox and provider result terminally `failed`.
  Preserve both records for diagnosis. The current product has no remote or
  automatic force-resend command; recovery requires a reviewed future tool or
  a new explicit user publication, not direct SQL mutation.

## Managed Codex socket

The managed app-server holds an exclusive mode-`0600` sidecar lock containing
only PID and process-start metadata. Startup refuses to unlink an existing
socket it cannot prove it owns.

At boot, do not use `[ -S PATH ]` as a readiness check. An abrupt host or WSL
stop may preserve the socket inode even though no process is listening. When an
optional rotating app-server and tlive share the default socket, install the
provided ordering drop-ins and require a successful bounded Unix connection
before tlive starts. This avoids two app-servers racing for one path without
adding `Requires=` coupling.

For a stale path, first verify locally that the recorded PID/start marker is
not a live matching process and that no process accepts the socket. Stop the
owning unit before any cleanup. If ownership remains uncertain, leave the path
in place and use the official stdio fallback or local recovery rather than
deleting it.

## Replacement hardware and writer leases

Completed SQLite state and provider session IDs can be restored with the local
configuration and project repositories. An in-flight turn is not portable.
Before resetting a `local` or legacy terminal writer lease on replacement
hardware, prove locally that the old host and its processes cannot still run.
The current schema does not store enough host identity to automate this proof,
so writer reset remains a reviewed local operation; Telegram cannot authorize
it.

## Queue rollback

1. Stop the selected external worker from taking new work.
2. Let safe work drain or classify remaining leases; do not cross an
   `executing` ambiguity boundary.
3. Keep the standalone sender running until prepared outbox rows are delivered
   or retained as explicit failures.
4. Change only the documented rollout gate. Do not remove queue tables or
   restore a migration backup for an ordinary runtime rollback.
5. Validate and run fault acceptance before resuming routine work.

A migration fault rolls its SQLite transaction back in place; it does not copy
the earlier backup over concurrent state. Manual backup restoration is reserved
for a separately proven database-integrity failure after all database users are
stopped. Runtime rollback retains accepted jobs, results, outbox rows, and
diagnostic history.

## Automated fault gate

Before a live queue cutover, run the full repository validation gate. Its
fictional subprocess matrix terminates child actors after Controller commit but
before offset persistence, during provider execution, and after fake Telegram
acceptance but before delivery persistence. It also covers pre-execution lease
recovery, same-root provider exclusion, explicit uncertainty resolution, and
separate Hub/provider polling offsets. This automated evidence does not replace
the owner-driven Telegram and
provider acceptance required for a deployment.
