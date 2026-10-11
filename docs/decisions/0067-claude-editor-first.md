# ADR 0067: Deliver Claude project reading and editing first

Status: accepted scope decision; implementation acceptance pending.
Date: 2026-10-10. Decision owner: repository owner.
Supersedes the full-parity delivery priority in the Claude continuation plan;
does not supersede security or other providers' contracts.

## Context

Full Claude parity combines project editing, remote permissions, native frontend
transfer, saved-session discovery and recovery. Several native compatibility
questions remain open despite published test prerequisites. The owner chose
reading and editing as the first useful delivery, with expansion only if actual
use demonstrates a need. Maintenance cost is part of that choice.

## Decision

The current Claude milestone is a Hub-owned project reader/editor using the
existing Read, Glob, Grep, Write and Edit slice. Its owning boundaries are
[REQ-AUTH-009 and REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
It retains exact Hub-owned sessions, one writer, visible waits/results/failures,
stop, durable recovery and the subscription route without paid fallback.
Configured model/effort selection remains available without claiming effective
effort, entitlement or unknown quota evidence.

Shell, test/build execution, Git commands, skills, MCP and child agents are
outside this milestone. Claude native `/local`/`/return`, saved-session connect,
image enablement and full provider parity are deferred until the owner identifies
a need and explicitly schedules an extension. They must not silently return as
prerequisites for the editor. Existing unsupported-transfer refusal and disabled
image defaults remain; no code, branch, worktree or evidence is deleted here.

Hub roles remain retired under [ADR 0065](0065-retire-hub-lead-advisor.md).
The independent outcome journal, other providers' control/access work, Hermes
operations, backups, refactoring, three-project acceptance and release preparation
remain separate milestones of the goal. Their contracts are not cancelled or
made new Claude-editor features by this decision.

## Acceptance and investment boundary

Use one finite matrix in the [Claude plan](../operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md).
Reuse the existing worker, queue/outbox, namespace and protected approval host.
First assess whether the supported project layout, immutable runtime and current
host can meet the existing boundary. Native Write/Edit must actually request and
consume a fresh exact human permission; configured manual mode alone is not proof.
Read/search remain confined to the project without requiring a card for every read.

Installed authority custody, real human approvals and subscription-route acceptance
remain mandatory. This choice grants no trust-model exception for unconfined
helpers or other launch paths. If the current boundary requires a new general
launcher, platform migration or broader permission infrastructure, report that
specific blocker and cost for an owner decision rather than automatically adding
another prerequisite workstream. Prepare concrete checks before seeking any
separately scoped live authorization.

## Consequences and closure

The editor cannot execute or validate its changes through a shell. Such work
uses a separately selected capable runtime with its own access and writer checks.
The current protected slice requires an ordinary Git root; linked worktrees with
a `.git` file are unsupported. Native use outside Hub does not gain Hub session
continuity or custody guarantees from this decision.

Close the editor milestone when its bounded matrix passes at one exact accepted
revision. Full parity is not completion evidence or an implicit next step.
No merge, deployment, service restart, provider call or live migration is
authorized by this record.
