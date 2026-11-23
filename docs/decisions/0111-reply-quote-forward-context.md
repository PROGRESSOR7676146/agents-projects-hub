# ADR 0111: Reply, quote and forward context reach the addressed turn

Status: accepted policy (owner request, 2026-11-23); implemented in source.
Release, installation and live Telegram acceptance are separate.
Date: 2026-11-23.
Amends: [REQ-UX-009 and REQ-CTX-006](../product/IDENTITY_AND_INTERACTION.md).

## Context

The owner replied to the summary that ended a previous session, in a new
session, and the agent did not see the message being answered. The Bot API
delivers that message inside `reply_to_message`, but Hub used it only to
address the replied-to bot. A selected quote did reach the provider, yet a
later part of a collected burst kept only its plain text and lost its quote.

Forwarding with a comment has a separate gap. Telegram delivers the comment
before the forwarded message. The comment starts a turn that collects its
burst for the configured quiet window, but the forward was stored as passive
context for the next turn, so the turn the owner meant never saw it.

## Decision

1. The replied-to message's text or caption, bounded to 4,000 characters, its
   author kind and the kinds of its non-text content form a labelled
   lower-priority quoted block of the addressed turn, next to the existing
   selected-quote block. The author kind is the user, a bot by username,
   another Telegram user, or a chat or channel by title; human names are not
   copied. Non-text content is named, not downloaded. A forum topic anchor is
   not a reply. A reply to a message in another chat states that its text is
   unavailable.
2. Every part of a collected burst carries its own Reply and quote blocks. When
   they do not fit the 20,000-character input, the blocks are shortened, never
   the user's text, and the cut is stated.
3. A forward joins the topic's tail turn as a labelled quoted part, with its
   materials, only while that turn still collects its burst: queued and
   unleased, inside its quiet and absolute windows, under the same session
   generation, model and Telegram ingress, with no root blocker and after any
   adoption boundary. Otherwise it stays passive and reaches the next addressed
   turn as before. A forward never starts a turn.
4. A forwarded message names its origin in the same way, without human names.
5. Routing, roots, policy, approvals and writers are unchanged: quoted, replied
   and forwarded text stays data, and the plain-Reply addressing rule stays as
   it was.

## Consequences

- A Reply turn can carry up to about 4 KB more input. Prompt injection through
  replied or forwarded text is the accepted trusted-installation risk; the
  text is labelled as data, not instruction.
- With batching off (quiet window 0) forwards stay passive, as before.
- A forward that arrives after its turn has started still reaches only the
  next turn; same-turn steering is not extended to forwards.
- The legacy inline execution paths and the Hermes plugin are unchanged.

## Verification and rollout

`tests/test_reply_forward_context.py` covers parsing, bounds, the plain Reply,
a quoted burst part, a forward joining a collecting turn exactly once, and a
late forward waiting for the next turn. Live Telegram acceptance with real
clients is tracked in the
[stabilization plan](../operations/STABILIZATION_PLAN.md).
