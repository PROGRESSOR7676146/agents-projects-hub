# Documentation map

Status: active  
Last updated: 2026-08-29

This is the durable navigation entry point for Agents Projects Hub. Product
intent belongs in the product requirements; verified current behavior belongs
in project status and executable tests; design rationale belongs in decisions;
operational procedures belong in runbooks. Historical session exports are
evidence, not current instructions.

## Start here

1. [Product requirements](product/PRODUCT_REQUIREMENTS.md) — mission, user model,
   requirements, lifecycle status, acceptance criteria, non-goals, and limits.
2. [Project status](status/PROJECT_STATUS.md) — what is implemented, verified,
   planned, deferred, rejected, and currently blocked.
3. [Architecture](ARCHITECTURE.ru.md) — current and earlier system boundaries.
4. [Decision map](decisions/README.md) — durable decisions and their rationale.

## Delivery and operation

- [Roadmap](ROADMAP.ru.md) — implementation sequence; it does not override the
  product requirements or the latest handoff.
- [Operations](operations/README.md) — installation, monitoring, recovery, and
  authoritative runbooks.
- [Recovery plane](RECOVERY_PLANE.ru.md) — Hermes and Agent Session Remote/tlive.
- [Security model](SECURITY.ru.md) and [security policy](../SECURITY.md).
- [Testing strategy](testing/README.md) — quality gates and live-test boundary.
- [Handoffs](handoffs/) — bounded assignments and continuity records.
- [Sanitized history](history/) — visible conversation evidence; never raw
  provider rollouts, tokens, hidden reasoning, or tool output.

## Legacy/current supporting specifications

- [Hub specification and plan](PROJECT_HUB_SPEC.ru.md) records the implemented
  pilot and earlier planning detail. Where it conflicts with the product
  requirements, the product requirements and current code/tests win.
- [Architecture](ARCHITECTURE.ru.md) and [roadmap](ROADMAP.ru.md) contain useful
  historical design detail. Status claims should be checked against
  [project status](status/PROJECT_STATUS.md).

## Truth rules

- Product outcomes and accepted boundaries: `docs/product/PRODUCT_REQUIREMENTS.md`.
- Current implementation behavior: code plus passing tests, summarized in
  `docs/status/PROJECT_STATUS.md`.
- Security invariants: `AGENTS.md`, `SECURITY.md`, and `docs/SECURITY.ru.md`.
- Consequential rationale: `docs/decisions/` and linked historical records.
- Planned work: `docs/ROADMAP.ru.md` and the latest accepted handoff.
- A handoff may narrow work but must not silently change durable product intent.
- Raw local configuration, databases, credentials, session rollouts, and private
  Telegram invite links are never documentation.
