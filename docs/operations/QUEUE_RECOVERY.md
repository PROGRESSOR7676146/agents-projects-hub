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
does not prove provider terminality. One explicit continuation may follow after
rechecking the attempt, current queue, session binding and delivered notice;
reconcile an unknown prior outcome before considering another invocation.

Before calling the root cause closed, separately verify the exact installed
revision and every required component, then run the coordinated independent-root
scenario in the [stabilization backlog](STABILIZATION_PLAN.md#live-acceptance-backlog).
Use only bounded method/count and binding metadata for private protocol attribution;
do not expose reasoning, prompts, tool payloads or raw protocol streams. Neither
quota status nor a restart alone proves the buffering fix is installed.

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
- An expired `sending` lease returns to `pending` through sender-scoped stale
  recovery. The provider result is not recomputed.
- Telegram may have accepted a message immediately before sender loss. A retry
  may therefore publish one bounded duplicate; this is safer than repeating a
  model turn or file-changing action.
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
