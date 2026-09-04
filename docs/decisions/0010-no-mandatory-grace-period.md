# ADR 0010: No mandatory grace period before productive work

## Status

Accepted. This decision supersedes the fixed 60-second default described in
REQ-UX-007; that oversized requirements document will be split before its next
safe revision.

## Context

A messenger-oriented agent should make its understanding and intended approach
visible for complex work. A previous proposal coupled that progress note to a
mandatory one-minute delay with Start, Clarify, and Cancel controls.

The delay adds latency, durable timer state, restart races, and another failure
mode to every complex task. Most work does not benefit from waiting merely in
case the user objects. It also makes the system feel less responsive while
providing no additional authority or safety guarantee.

## Decision

- A complex task receives a concise progress note when interim transport is
  available, then proceeds without an artificial grace period.
- Simple, unambiguous tasks start immediately.
- Hub pauses only for a material ambiguity, missing authority, destructive
  decision, explicit planning request, or direct user request to wait.
- Start, Clarify, and Cancel controls may be offered for a real pending
  decision; they are not a mandatory stage of ordinary execution.
- Emergency stop remains deterministic and independent of model analysis.

## Consequences

Telegram work starts faster and requires less orchestration state. Agents must
still expose consequential assumptions early enough for steering or emergency
stop, and must challenge product suggestions whose operational cost exceeds
their value rather than converting every suggestion directly into a feature.
