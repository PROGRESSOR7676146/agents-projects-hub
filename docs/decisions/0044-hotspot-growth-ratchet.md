# ADR 0044: Hotspot growth ratchet

Status: accepted
Date: 2026-09-27

## Context

Maintenance rules 10–12 require a bounded exception whenever a change touches
a module of roughly 1,500 lines or a function of roughly 200 lines. Nothing
checked them. After the refactoring plan closed, seven commits grew
`service.py` and `state.py` without any recorded exception, and
`_handle_update` reached 876 lines.

## Decision

`hermes_codex_router.hotspot_audit` runs as a cheap stage of every validation
profile. It measures every package module of at least 1,500 lines and every
function of at least 200 lines. Each hotspot needs an entry in
`docs/operations/hotspots.json` with `max_lines` and the rule-11 fields:
rationale, owner, next review and reopening event.

A new hotspot without an entry, or a hotspot larger than its recorded bound,
fails validation. Raising a bound in the same change is the bounded exception
and is visible in review. A hotspot that shrank, an entry whose target is gone
and an overdue review are reported as debt without failing, so the gate never
breaks on a date or on an improvement.

Maintenance rules 12 and 14 now name this ratchet; size remains a
review-selection signal, not a quality score.

## Consequences

Any growth of an existing hotspot now forces an explicit, reviewed decision.
Mechanical splitting to pass the check stays prohibited by rule 12. The first
baseline records 21 hotspots at their current sizes, reviewed by 2026-12-31;
stabilization stage 3 targets the dispatcher, configuration and CLI entries.
