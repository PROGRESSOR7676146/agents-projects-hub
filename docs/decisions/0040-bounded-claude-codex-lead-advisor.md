# ADR 0040: Bounded Claude Code/Codex lead and advisor workflow

Status: accepted scope; implementation and live acceptance pending
Date: 2026-09-27

## Context

Both providers should help one owner plan and review work in a project topic.
A general multi-agent platform would add role arbitration, recursive delegation,
cross-provider context propagation and recovery paths before their value is
observed. More worker processes alone do not resolve write authority on a Git
root. The initial text-only Claude CPA worker is an execution scaffold, not a
write-capable lead or an isolated advisor.

## Decision

The first collaboration release has one human-selected lead and one advisor in
an explicitly enabled topic workflow. Either Codex or Claude Code may lead. The
lead alone may change the project; the advisor may read authorized material and
return visible criticism. Role enforcement must come from runtime permissions
and ownership, not a prompt. The initial advisor has no subagents; optional
lead helpers are one level of read/search/analysis only.

The workflow is sequential: the lead finishes, Hub durably hands one bounded
question and exact result reference to the advisor, then gives the lead one
continuation. Hub never holds the lead's FIFO slot while waiting for the advisor
and never routes bot-to-bot through Telegram. Role changes occur at a safe idle
boundary without retargeting accepted jobs. Canonical-root writer exclusion
remains in force across topics; parallel writers in one root are deferred.

The owner initially chooses roles, model and effort. A minimal outcome journal
records observable results and owner decisions. Automatic scores, model judges,
comparative duplication and learned dispatch are deferred until evidence shows
their benefit. Claude uses the operator-selected subscription path through a
local CPA route; private route acceptance must exclude paid/API and extra-usage
fallback. The existing Controller, workers, SQLite queue/outbox and ownership
rules remain the platform. No additional orchestrator, database or broker is
required for this milestone.

The observable contracts live in [REQ-COLLAB-001..002](../product/IDENTITY_AND_INTERACTION.md#explicit-claude-codecodex-collaboration),
[REQ-WRITER-013](../product/ACCOUNTS_CONTROL_AND_SECURITY.md#11-frontends-writer-lease-and-local-transfer),
[REQ-QUEUE-011](../product/PERSISTENCE_AND_RECOVERY.md#implemented-queue-compatibility-and-local-provider-worker-isolation),
and [REQ-EVAL-010](../product/EVALUATION_AND_ALLOCATION.md#21-evaluation-and-resource-aware-task-allocation).
The [implementation plan](../operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md) owns
sequencing and open capability checks, not another normative contract.

## Consequences

Three Codex and three Claude worker slots can serve independent projects, but
do not grant two simultaneous writers in one root. The first useful milestone
depends on proof of human Claude approvals, advisor read isolation, exact
session continuity and CPA account policy. The text-only worker and offline
tests do not establish that acceptance. Later parallel-write or automated
allocation work needs a separate decision and evidence.
