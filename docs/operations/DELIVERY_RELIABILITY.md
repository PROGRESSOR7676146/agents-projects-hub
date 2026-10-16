# Delivery reliability implementation plan

State: source candidate under validation; Hub maintainer owns integration.
Last verified base: `a6fddf3941f3ec56b623a7fa73dfb657d8dc9c36`.
[ADR 0069](../decisions/0069-late-receipts-and-notice-ordering.md) defines the
schema54 receipt and notice-ordering increment. Next trigger: exact-candidate
independent review, canonical/hosted publication gates, compatible rollback and
coordinated activation before the postponed dedicated-account live scenarios.
Offline synthetic delays and SQLite faults are not Telegram acceptance.
Closure remains open until those deployment-local checks pass. Preserve the
separate owner-driven approvals, native transfer and multiworker maintenance
boundaries in the existing live backlog.


Publication checks belong to the [testing guide](../testing/README.md).
