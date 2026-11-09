# ADR 0097: Reply choices as Telegram buttons

Status: accepted, 2026-11-09. Source implementation is separate from installed acceptance.

## Context

The Telegram contract asked providers to state "option labels suitable for
inline buttons", but no component turned them into buttons, so the owner saw
only text. REQ-UX-006 required that such buttons stay persisted and bound to the
owner, topic and session, and that model-supplied commands never execute.

## Decision

A provider ends a visible answer with one last line `[[choices: A | B]]`: 2–6
distinct printable labels of at most 40 characters, outside an open code fence.
Hub removes a valid line from the answer; an invalid one stays visible text.

The labels travel with the formatted answer and become one extra final outbox
part after every text part and file, bound to the job's Hub session by a short
public digest. The final sender delivers that part as a short prompt with an
inline keyboard through the Hub bot, because callbacks reach only the bot that
sent the message and the Controller polls the Hub bot. No schema change is
needed; an older release shows the stored part as text.

A press is handled by the Controller after the usual sender authorization and
topic resolution. The label is read from the pressed message itself. The press
must name the topic's current Hub session (after `/new` old buttons are refused),
and the first press per message wins through the persisted callback claim. The
keyboard is then replaced by one "✓ label" mark and the label enters normal
ingress as the sender's ordinary text reply. A press therefore grants nothing
beyond typing the same words: no command, path, approval or authority comes
from the model or the button.

## Consequences

- Choices work in project topics. Direct-message choices are not implemented.
- Buttons are sent by the Hub bot while the answer comes from the provider bot.
- A failed keyboard edit leaves stale buttons visible; the claim still refuses them.
- Installed and live Telegram acceptance remain pending.
