# ADR 0005: Telegram interaction is a shared provider contract plus Hub UI

Status: accepted

## Context

Provider-native agents otherwise assume a terminal-style conversation. A
Telegram user benefits from concise conversational responses, focused
clarification, replyable message parts, visible progress, buttons, reactions,
and attached artifacts. Prompt text alone cannot perform or confirm Telegram UI
operations, while hard-coding all conversational judgment in the deterministic
Controller would require another routing model and violate provider isolation.

## Decision

The product separates semantic and transport responsibilities:

- a versioned shared prompt contract tells every provider that the visible
  conversation uses Telegram;
- bounded runtime-specific notes tune presentation without changing authority;
- new provider sessions receive the full contract and existing sessions receive
  a compact reminder;
- models decide meaning, clarification, task execution, and visible wording;
- the Hub alone owns reactions, chat actions, message parts, callbacks,
  attachments, delivery confirmation, and durable grace-period state;
- Telegram-owned receipt ticks are not imitated with a bot reaction, and native
  `Thinking…` drafts are used only where the Bot API supports them;
- a model statement never proves that a Telegram UI operation occurred.

The Controller remains deterministic. A future grace-period workflow may use
the selected provider to form an intent, but its timer, correction, cancellation,
and authorization boundaries must be durable local state rather than an in-model
sleep or an independent routing model.

## Consequences

The common contract is consistent across providers and inexpensive after
session initialization. Existing sessions adopt the behavior without being
discarded. UI operations remain testable and retryable independently of model
output. Automatic GIF behavior is excluded. Multipart replies, artifact delivery, bounded choice callbacks, and the
60-second reversible-work grace period require separate transport work and are
therefore tracked explicitly instead of being simulated by prompt prose.
