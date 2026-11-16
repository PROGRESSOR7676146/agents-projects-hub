# ADR 0105: The first boot start leaves queued Codex work in place

Status: accepted, 2026-11-16 (owner decision). Source implementation is separate
from installed acceptance.
Amends: [ADR 0088](0088-boot-time-codex-server-start.md),
[ADR 0104](0104-boot-start-reconciles-interrupted-codex-executions.md) and
REQ-OPS-005.

## Context

After [ADR 0104](0104-boot-start-reconciles-interrupted-codex-executions.md),
the first start attempt of a boot settles a Codex execution that a host crash
interrupted. Work queued behind it still refuses the start. That work is often
a message that arrived while the interrupted turn ran. The refusal spends the
boot's only receipt-less attempt, so the server stays down until another
reboot or a manual receipt start. Workers then run the queue through their
stdio fallback, and tlive cannot attach.

A `queued` or `retry_wait` job holds no native turn. A job enters these states
only before it executes: at enqueue, from a pre-execution lease, or after
proof that an input was never accepted. A stale `executing` job becomes
`indeterminate` and never returns to the queue. Starting the server cannot
change such a job. The real risk is a consumer that leases the job while the
server takes custody. Only the external worker of the `codex` agent and the
embedded consumer of the Controller and the Telegram bot lease Codex jobs.
At boot their units are ordered `After=` the server unit and wait for its
start job.

## Decision

1. Only the receipt-less first start attempt that ran the ADR 0104
   reconciliation qualifies. Receipt starts, and manifests without
   `hub_config`, keep the full idle gate.
2. After the reconciliation and its repeated native-client and server
   refusal, the attempt takes one more same-UID process inventory. It looks
   for any Hub process that could lease a Codex job. The check fails closed:
   - A process that mentions the Hub counts as a consumer unless its entry
     sits at a proven program position (the program itself, an interpreter
     running it with no option in front, or `python -m`) and parses as a
     known non-consumer:
     - `sender`, `monitor` or `project-provisioner`;
     - `worker` with exactly one unabbreviated `--agent` value other than
       `codex` (the worker defaults to `codex`);
     - the socket helpers;
     - this start gate and its readiness step.
   - Codex workers, the Controller, the Telegram bot, unknown commands and
     ambiguous arguments all count as consumers.
   - Unreadable process custody refuses the start, as it already does.
3. When no consumer is observed, the idle gate ignores `queued` and
   `retry_wait` Codex jobs. `leased` jobs, even with an expired lease, and
   `executing` jobs still refuse. So do unsettled controls, live read claims and
   non-Telegram writers. When a consumer is observed, the journal names its
   PID and the gate is unchanged, so queued work refuses as before.
4. Nothing writes to queued work. It runs after readiness through the ordinary
   workers over the socket. The existing FIFO and indeterminate-scope rules
   still hold back work behind a job that the reconciliation marked
   indeterminate. There is no new state, schema or unit change.

## Consequences

- A crash with work queued behind the interrupted turn no longer leaves the
  server down after the reboot, provided the consumers wait for it.
- The inventory is a snapshot, not an admission fence. A consumer that starts
  after it, by hand or from a unit that is not ordered after the server, can
  lease queued work before readiness. That is the ADR 0088 limitation. In
  receipt mode the queued refusal is itself only a snapshot.
- The non-consumer list is a contract. If a command on it gains a queue
  consumer, it must leave the list. A Hub process of an unknown form only
  turns the exception off. It never weakens another gate.
- No owner, Telegram, provider or model action is added, and nothing is
  replayed. An older runtime keeps refusing queued work.

Live boot acceptance is pending: crash a fictional turn with a fictional
message queued behind it, reboot, and verify that the server starts, the
interrupted job settles once without replay, and the queued message then runs
once.
