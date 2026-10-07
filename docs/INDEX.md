# Documentation map

This repository documents only reusable product behavior. Deployment inventory,
operator identities, real project names, session exports, and live transcripts
are intentionally excluded.

## Start here

Follow the scoped read order in [AGENTS.md](../AGENTS.md).
The [product index](product/PRODUCT_REQUIREMENTS.md) routes capability reading;
[maintenance](product/MAINTENANCE.md#19-maintenance-and-change-policy) defines
document ownership. [Project status](status/PROJECT_STATUS.md) records evidence,
and the [decision map](decisions/README.md) routes durable rationale.
The completed [requirements split](product/REQUIREMENTS_SPLIT_PLAN.md) is
migration rationale, not a routine prerequisite or another specification.

For Claude Code/Codex integration, read the accepted
[lead/advisor plan](operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md) and its
[next development task](operations/NEXT_DEVELOPMENT_SESSION.md) after the
baseline above. Check [status](status/PROJECT_STATUS.md) for the implemented
subset; the plan's accepted role choices are not a claim of runtime parity.

## Delivery and operation

- [Stabilization plan and live-acceptance backlog](operations/STABILIZATION_PLAN.md)
- [Roadmap](ROADMAP.ru.md)
- [Next development session](operations/NEXT_DEVELOPMENT_SESSION.md)
- [Claude Code and Codex: bounded lead/advisor plan](operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md)
- [Claude protected file-tool candidate](operations/CLAUDE_FILE_PERMISSIONS.md)
- [Claude authority custody preparation](operations/CLAUDE_CUSTODY.md)
- [Exact-job outcome diagnostics](operations/OUTCOME_JOURNAL.md)
- [Inbound materials implementation plan](operations/INBOUND_MATERIALS_PLAN.md)
- [Operations](operations/README.md)
- [Queue and process recovery](operations/QUEUE_RECOVERY.md)
- [Live canary and rollback](operations/LIVE_CANARY.md)
- [Release metadata synchronization](operations/RELEASE_METADATA.md)
- [Recovery plane](RECOVERY_PLANE.ru.md)
- [Reciprocal recovery capsules](operations/RECOVERY_CAPSULES.md)
- [Off-machine WSL recovery plan](operations/WSL_OFF_MACHINE_RECOVERY.md)
- [Risk register](risks/RISK_REGISTER.md)
- [Quality and stability review](operations/QUALITY_AND_STABILITY_REVIEW.md)
- [Reliability implementation plan](operations/RELIABILITY_PLAN.md)
- [Incremental refactoring plan](operations/REFACTORING_PLAN.md)
- [CLI-to-Telegram session transfer implementation plan](operations/SESSION_TRANSFER_IMPLEMENTATION_PLAN.md)
- [Saved Codex session connection](operations/SESSION_CONNECT.md)
- [Project/group onboarding](operations/PROJECT_GROUP_ONBOARDING_PLAN.md)
- [Testing and privacy gate](testing/README.md)

## Research proposals

- [Agent scoring and resource-aware allocation](research/AGENT_SCORING_AND_ALLOCATION.ru.md)
  — metrics, comparative trials, subscription constraints and Jev assessment;
  the [product foundation](product/EVALUATION_AND_ALLOCATION.md) is accepted,
  while methods and implementation remain subject to validation.
- [Multi-provider agent collaboration](research/MULTI_AGENT_COLLABORATION.ru.md)
  — non-normative research on team chat, coordination protocols, isolated work,
  delegation, and acceptance; not an implemented or accepted product contract.

## Truth rules

- Product outcomes and boundaries: `docs/product/PRODUCT_REQUIREMENTS.md` and
  every normative module linked from it.
- Current reusable behavior: code, passing tests, and
  `docs/status/PROJECT_STATUS.md`.
- Security and publication invariants: `AGENTS.md`, `SECURITY.md`, and the
  mandatory privacy scan.
- Deployment-specific evidence is private operational state and never a source
  file in this repository.
