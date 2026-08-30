# Documentation map

This repository documents only reusable product behavior. Deployment inventory,
operator identities, real project names, session exports, and live transcripts
are intentionally excluded.

## Start here

1. [Product requirements](product/PRODUCT_REQUIREMENTS.md)
2. [Project status](status/PROJECT_STATUS.md)
3. [Security model](SECURITY.ru.md) and [security policy](../SECURITY.md)
4. [Decision map](decisions/README.md)

## Delivery and operation

- [Roadmap](ROADMAP.ru.md)
- [Operations](operations/README.md)
- [Queue and process recovery](operations/QUEUE_RECOVERY.md)
- [Recovery plane](RECOVERY_PLANE.ru.md)
- [Risk register](risks/RISK_REGISTER.md)
- [Testing and privacy gate](testing/README.md)

## Truth rules

- Product outcomes and boundaries: `docs/product/PRODUCT_REQUIREMENTS.md`.
- Current reusable behavior: code, passing tests, and
  `docs/status/PROJECT_STATUS.md`.
- Security and publication invariants: `AGENTS.md`, `SECURITY.md`, and the
  mandatory privacy scan.
- Deployment-specific evidence is private operational state and never a source
  file in this repository.
