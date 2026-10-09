# ADR 0039: Limited Claude Code workers through a local CPA route

Status: repository scaffold; deployment acceptance pending
Date: 2026-09-27

## 2026-10-09 scope amendment

[ADR 0065](0065-retire-hub-lead-advisor.md) withdraws the planned lead/advisor
capability and advisor-isolation prerequisite below. Standalone Claude human
approvals, general provider/helper custody, exact sessions and route acceptance
remain required. The original scaffold rationale is retained.

## Decision

The existing external queue owns Claude Code CLI turns. `claude_worker_count`
configures 1–16 independently locked processes, with three intended for the
first multi-project rollout. Every process owns its adapter and SQLite
connection. The transactional global root limit, canonical-root exclusion,
fairness, draining reduction, cached health and exact revision check apply to
Claude and Codex together. The shared sender owns Claude result delivery.

The first adapter deliberately permits text-only turns. It uses documented
headless structured output and exact `--resume`, accepts one successful terminal
result with the same session UUID, and never publishes assistant thinking. It
requires an explicit loopback CPA route and one client credential before launch.
`--safe-mode`, `--restricted`, an empty built-in tool list, strict MCP selection,
disabled skills and denied permission prompts keep this scaffold from writing
without an accepted human approval channel. Unknown completion or session drift
retains the existing indeterminate job boundary; no automatic replay follows.

## Limits and next decision

An environment preflight does not attest the listener or CPA upstream routing.
Private deployment must verify the selected CPA account and exclusion of paid
fallback before productive use. The current adapter is not the promised Claude
lead or advisor: it cannot edit, review repository files through tools, or accept
human approvals. That capability requires an explicit approval host and
independent read isolation; neither can be inferred from model instructions or
CLI flags alone. Local transfer, saved-session connection, partial progress and
passive quota data also need separate acceptance. No service is enabled by this
repository change.

This extends [ADR 0038](0038-multiple-codex-worker-slots.md) without changing
Codex process ownership. The queue state layer still owns the lease transaction,
the worker owns provider invocation and stop, and the sender owns delivery.
Offline tests prove admission and parser behavior. Exact-revision publication,
CPA/provider live checks and Telegram acceptance are later gates.
