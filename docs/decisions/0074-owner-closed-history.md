# ADR 0074: Owner-closed historical blockers

Status: source candidate; independent review and live acceptance pending
Date: 2026-10-26

## Decision

Reuse the existing immutable owner resolution rather than add a repair ledger,
change uncertain jobs to completed, or reconstruct unavailable historical work.
The owning contracts are [REQ-OPS-005](../product/PERSISTENCE_AND_RECOVERY.md)
and [REQ-QUEUE-014](../product/DURABLE_QUEUE_AND_CONTROL.md).

The independent-server admission guard previously ignored archived owner-resolved
jobs and effective final-delivery consent. It now interprets those existing
records without modifying them. Current queued, retry-wait, leased and executing
work, unresolved current generations, active read claims and local writers
remain blockers. A start receipt still cannot establish native-client custody.

An uncertain interrupt of an independently proven terminal exact turn cannot
become authority for another turn. An explicit owner resolution can therefore
retire that target's effective root blocker without claiming its sender exited.
Dependency-neutral SQL in `codex_control_predicates` owns this interpretation
for admission, control changes, transfers, adoption, relocation, lanes and drain.
The retained send fence continues to prohibit every repeat interrupt. The
provider job, control row, terminal evidence and delivery records remain intact.
No background disposition or native invocation is added. Unfinished read claims
continue blocking until their existing owner/cleanup settles them.

HubState retains transaction ownership. The only mutation is the existing
owner-only `indeterminate-resolve` operation. No migration or schema bump is
needed. A compatible older runtime may conservatively reinstate the blocker;
rollback must not erase owner decisions or other newer state.

## Evidence and limits

Offline tests cover archived resolved/unresolved history, live generations,
retry work, exact terminal control closure and its retained raw unknown/fence,
read claims, local writers and exact final-delivery consent. Existing control
race and fence tests remain mandatory. Independent source review and exact-head
publication gates precede use. These fixtures establish no installed revision,
human Telegram delivery or server cutover.
