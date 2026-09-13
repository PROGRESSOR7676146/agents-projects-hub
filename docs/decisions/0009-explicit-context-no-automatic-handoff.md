# ADR 0009: Explicit context without automatic handoff

Status: accepted  
Date: 2026-09-03

## Context

The Hub records bounded visible turns so a user can ask one provider to inspect
work previously discussed with another. Earlier routing automatically injected
unseen dialogue and staged model-generated summaries during provider or model
switches. That behavior spent tokens without a direct request, made a switch
semantically surprising, and could present stale dialogue as relevant work.

## Decision

Provider and model switches change only deterministic local routing/session
state. The database rejects creation of pending handoffs, and all runtime paths
ignore legacy handoff records. Ordinary productive messages contain no unseen
inter-agent journal delta.

The visible journal remains bounded by numeric topic identity. A user may issue
the advanced command `/context [agent_id] [1..20]` to send a repeatable snapshot
to the currently addressed provider. The command is omitted from the compact
Telegram menu. Retrieved text is explicitly labelled as lower-priority visible
conversation context. Forwarded Telegram messages remain a distinct passive
quote mechanism and are never interpreted as commands.

## Consequences

- Switching providers is fast, deterministic, and consumes no model tokens.
- A new provider starts without hidden assumptions unless the user requests
  history.
- Users must explicitly request context when continuity across providers is
  desirable.
- Legacy handoff tables and nullable job fields remain temporarily for database
  compatibility, but new pending handoffs are prohibited by schema triggers.
