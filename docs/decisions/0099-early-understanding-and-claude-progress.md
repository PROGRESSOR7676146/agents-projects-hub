# ADR 0099: Early understanding and Claude progress in Telegram

Status: accepted, 2026-11-11 (owner request). Source implementation is separate from installed acceptance.

## Context

Messenger requests are often short, lack context or contain typos. The owner
needs to see quickly how an agent understood a request, so that a wrong
direction is corrected before much work is done, and needs regular updates,
because nothing else of the agent's work is visible through Hub.

Codex already delivered its native interim "commentary" through the durable
progress outbox. Claude wrote interim text too, but Hub recorded it only as a
journal item of unknown phase and delivered nothing until the final answer.
Claude also received the older v1 contract, whose wording made an early
understanding message conditional on the runtime's ability to publish one.

## Decision

1. Telegram contract v3 applies to Codex and Claude. It asks for an interim
   message within the first minute or two of a new request that states, in the
   agent's own words, the goal, why it matters, the relevant context, what will
   and will not be done, and the approach and means; a short "understood,
   checking the materials" note first when a quick look is needed; progress at
   meaningful milestones and at least every ten minutes; never silent until the
   final answer. The agent keeps working without waiting for confirmation unless
   an ambiguity materially changes the result. The version bump makes existing
   sessions receive the full contract on their next turn. Other runtimes keep v1
   unchanged.
2. A Claude main-thread text is interim when its native request (`message.id`)
   later carries a tool call, or when a newer native request starts. Hub then
   moves that recorded journal item once from `unknown` to `commentary` and
   queues it in the existing progress outbox (external outbox only), with the
   existing rate limit, silent notification and supersession by the final
   result. The last text before the terminal result is never promoted, so the
   final answer is not duplicated. Subagent texts and streamed corrections are
   excluded. A failed promotion never changes the turn's outcome.
3. The Claude and Codex transport notes explain the progress channel and carry
   the cost-aware practice of ADR 0098 (one task per session at task
   boundaries, short tool output).

## Consequences

- The owner sees Claude's first understanding seconds after it is written,
  provided the model writes it before its first tool call; the contract asks it
  to. Timing still depends on the model.
- The progress outbox keeps at most one update per two minutes per job; more
  frequent interim texts are not delivered, as for Codex.
- A Telegram message already delivered as progress cannot be retracted.
