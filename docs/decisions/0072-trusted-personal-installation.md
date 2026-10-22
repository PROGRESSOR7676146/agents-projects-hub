# ADR 0072: Trusted personal installation and finite Claude acceptance

Status: accepted owner scope decision; implementation and live acceptance pending.
Date: 2026-10-22. Decision owner: repository owner.
Contracts: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md),
[REQ-QUEUE-015](../product/DURABLE_QUEUE_AND_CONTROL.md),
[REQ-EVAL-009/010](../product/EVALUATION_AND_ALLOCATION.md).

## Context

The earlier custody gate required proof that every model-controlled productive,
helper, administrative and recovery launch could not reach Hub/tlive authority.
A confined Claude child does not prove that property for its same-UID host.
A complete answer would require broader host-management isolation, with cost and
maintenance outside the first useful editor. Existing source checks remain useful
without that global promise. The owner chooses a trusted personal installation.

## Decision and precise supersession

The installation has one decision owner and trusted assistants with explicitly
assigned administrative/recovery rights. Global exclusion of all same-UID/admin
assistants from keys, state, receipts and endpoints is not claimed and is no
longer an editor/journal release prerequisite. No VM, UID/MAC migration or general
model launcher is added to satisfy it. The global inventory/study is stopped.
An unconfined same-UID or privileged process can read private authority, forge
symmetric receipts or interfere with execution. Mode 0600 and prompt policy do
not prevent that. Shared networking does not guarantee exfiltration isolation.
Trust is operational, not prompt-injection resistance. Model turns outside the
confined Claude namespace, including Codex workspace-write, OpenCode, Antigravity
and native Hermes tools, remain within this risk; their sandbox/tool flags do not
prove Hub/tlive authority exclusion. Scoped installed authority-listener and CPA
client/management separation checks remain mandatory under REQ-SEC-008.
These are accepted residual risks, never passed isolation evidence.

This partially supersedes ADRs [0052](0052-protected-claude-file-permissions.md)
and [0053](0053-claude-custody-reference-deployment.md) only for the global custody
prerequisite, including the corresponding legacy custody statements in ADRs
[0039](0039-claude-cpa-worker-scaffold.md),
[0056](0056-provider-neutral-process-namespace.md) and
[0065](0065-retire-hub-lead-advisor.md); their adapters/shared primitives and role
retirement remain unchanged. ADR [0055](0055-managed-codex-profile-continuity.md) only for global
helper/tool custody; ADR [0067](0067-claude-editor-first.md) for its no-trust-exception
clauses and editor closure. ADRs [0070](0070-worker-operations-drain-control.md)
and [0071](0071-worker-socket-thread-custody.md) retain physical endpoint/thread
ownership; ambiguous installed/restoration custody means scoped checks, not
host-wide assistant exclusion. Historical exposure findings remain valid.

The confined Claude launch keeps namespace/private-path exclusion, immutable
runtime/hook, read-only ordinary `.git`, separate session HOME and only
Read/Glob/Grep/Write/Edit. Linked worktrees remain unsupported. Every native
Write/Edit needs a fresh exact human permission. Human-only approval,
first-valid-answer, fail-closed ambiguity/restart/timeout, immutable numeric
origin/root/session identity, one Hub-owned writer, queue/outbox and no uncertain
replay remain. A failed namespace cannot fall back to unconfined execution.
Codex retains its supported workspace-write and human approval policies.
Other providers retain their supported tools; this decision does not weaken or
expand their adapter permissions. Stronger hostile-agent isolation is a later
explicit scope decision, not hidden debt of this installation.

Claude editor closure uses the finite [editor matrix](../operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md)
on one ordinary disposable root at an exact accepted revision. Native/human
permissions, scoped installed checks, exact fresh launch/resume, stop/results/
delivery recovery and subscription without paid/API/extra-usage fallback remain
required. Three-root/core/restart acceptance is a separate milestone. Shell,
tests/build, Git commands, skills/MCP/children, images, local/return, saved-session
connect and full Claude parity stay deferred. Hub lead/advisor roles stay retired.

Hermes remains an independent native product and a project participant. Its
plugin/hook, channels, providers and tools are retained. Hub imports/schema and
Gateway source/environment pins remain real compatibility dependencies; rollback
must coordinate them. Existing deterministic model-free monitor repair remains
installation-authorized, with its state/intervention recorded in recovery drills.
Reciprocal repair uses peer-owned capsules and explicit task scope, not approval
by another agent. Arbitrary native/admin writers require operational coordination;
Hub leases do not prove host-wide writer exclusion. No plugin removal, second
Gateway or tool reduction follows from this decision.

## Recovery and delivery consequences

Backups restore only captured data. They do not undo disclosed secrets, external
effects or forged authority, and do not automatically restore trust in receipts.
Suspected compromise needs separately authorized authority checks/credential
rotation. Full host cold restore remains external infrastructure. A bounded
Hub-consistent restore stays a Hub milestone. Runtime rollback uses a distinct
compatible executable against retained current-schema state, never an older DB
over accepted jobs and deliveries.

REQ-OPS-015 remains unchanged for the planned independent stack updater: a model
turn cannot stage, apply or roll back; apply/rollback require serialized single-use
owner control bound to the exact plan/current links with bounded expiry and no
survival across Gateway restart. Hub releases retain REQ-OPS-010/011. Ordinary
monitor/reciprocal repair is not a version/link/source-pin switch authorization;
that coordination uses the separately authorized owning release procedure.
No new framework, schema,
scheduler, live call, installation or deployment is authorized by this ADR.
Source review, canonical/hosted gates and scoped live authorization stay separate.
The [stabilization plan](../operations/STABILIZATION_PLAN.md#trusted-installation-execution)
owns sequence and checkpoint reporting; runbooks own procedures rather than a
second behavioral contract.
