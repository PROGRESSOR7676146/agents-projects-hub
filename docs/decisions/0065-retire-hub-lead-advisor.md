# ADR 0065: Retire Hub-managed lead and advisor roles

Status: accepted scope change; documentation implementation
Date: 2026-10-09
Supersedes: [ADR 0040](0040-bounded-claude-codex-lead-advisor.md), for the
Hub-managed collaboration milestone only.

## Context

Independent cross-provider review can be organized by project instructions and
explicit provider calls. A second orchestration layer in Hub would duplicate
that workflow while adding role authority, material handoff, continuation,
recovery and isolation state. The owner withdrew this product feature from the
current development goal and release scope.

## Decision

Hub does not implement lead/advisor assignment, role handover, advisor-only
permissions or the bounded review/continuation workflow. REQ-COLLAB-001..002,
REQ-WRITER-013, REQ-QUEUE-011 and AC-F-015 are retired; their identifiers remain
as tombstones and must not be reused. They are removed from release acceptance,
not counted as implemented work.

Cross-provider review belongs to project rules and explicitly authorized agent
work. Hub does not schedule, enforce or certify that review. Prompt instructions
do not establish filesystem isolation or grant approval authority. Existing
development requirements for independent review remain in force.

Claude remains an independent native provider. Human approvals, exact sessions,
model/effort selection, local transfer, saved-session connection, recovery,
visibility and subscription/no-paid-fallback acceptance stay in scope. General
outcome/usage diagnostics under REQ-EVAL-010 remain independent of collaboration.
The existing canonical-root writer exclusion and launch/helper/MCP/service-data
custody requirements are unchanged. No additional writer or bypass is enabled.

Advisor-only capsule, private inference-pipe and combined native/pipe workflow
development stops. Existing source, tests and review evidence are preserved;
general namespace, CLI, cleanup and security fixes may be integrated only on
their independent merits. This decision does not merge, close, delete, deploy
or restart any existing candidate, branch, worktree or service. Dependency
cleanup is an explicit inventory/review step, not an automatic replay or merge.

Scoring, model judges, learned allocation and parallel writing agents remain
outside this goal. Reintroducing Hub roles requires a new owner decision based
on a demonstrated product need.

## Consequences

The remaining plan loses one substantial lifecycle and isolation workstream.
Provider safety, Claude parity, operational work and live acceptance still
determine readiness. Progress estimates use the reduced scope; withdrawal is
neither completion evidence nor deployment acceptance.

In-scope Claude process observations and Codex ingress/control-loss candidates
descend from advisor foundations/sequencing. Before owner integration, choose
explicitly whether to retain dormant primitives/CI or split the necessary
changes; neither is automatic. The checked lanes, PRs and revisions are in the
[dependency inventory](../operations/RETAINED_PROVIDER_DEPENDENCIES.md).

## Affected records

ADR 0040 is superseded. The advisor-only prerequisite in ADR 0039 and future
integration triggers in ADRs 0056–0058 are withdrawn, while their reusable
provider/security decisions remain. Retired IDs are recorded in the identity,
writer, queue and acceptance modules; REQ-EVAL-010 stays independently owned
with its [acceptance boundaries](../product/EVALUATION_AND_ALLOCATION.md#acceptance-boundaries).

Owning contracts remain in the [product baseline](../product/PRODUCT_REQUIREMENTS.md).
The [Claude plan](../operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md) and
[next session](../operations/NEXT_DEVELOPMENT_SESSION.md) own the new sequencing.
The [stabilization plan](../operations/STABILIZATION_PLAN.md#revised-delivery-scope)
owns the reduced goal table.
