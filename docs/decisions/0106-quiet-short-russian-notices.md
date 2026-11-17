# ADR 0106: Quiet, short Russian notices

Status: accepted, 2026-11-17 (owner decision, stabilization plan step 2).
Source implementation is separate from installed acceptance.
Amends: [ADR 0051](0051-accepted-turn-activity-and-queue-notices.md) and
REQ-QUEUE-012.

## Context

Each request produced up to three Hub notices before any answer: acceptance,
a queue snapshot and the execution handoff. Most of them reported a normal,
seconds-long transition. Failure and uncertainty notices were English and
used a three-part "What happened / Saved / Next" template of 500–900
characters. Some offered an action that the current state then refused: the
uncertain non-Codex notice asked for a new message while the same
uncertainty blocked the project. The owner reads these on a phone.

## Decision

1. **Quiet transitions.** Acceptance and the execution handoff are still
   recorded, so opt-in, history and supersession keep working, but they are
   stored as superseded and never sent. A queue snapshot that needs an owner
   decision or action (`held`, `delivery_unknown`, `root_held_fifo`,
   `root_uncertain`, `root_writer`) is due at once. Other wait reasons become
   due only after `QUEUE_NOTICE_DELAY` (120 s). The existing first-send check
   supersedes the snapshot unsent if the job leaves the queue first.
   Same-topic FIFO and input collection are recorded but never sent, because
   the blocker is visible in that topic or the batching is deliberate.
   No-progress, approval and delivery warnings are unchanged in timing.
2. **Copy.** Owner-facing failure, uncertainty, blocker and status notices are
   Russian. Each says what happened, what it means and one action. A status
   notice is at most 220 visible characters and a failure or blocker notice at
   most 400, excluding collapsed content. An uncertainty notice states that
   files may have changed, that there is no automatic retry, and that the
   project is paused. It never suggests an action that the pause would refuse.
3. **Collapsed detail.** Explicitly incomplete provider text keeps its
   existing bound but moves into a Telegram expandable quote after the action.
   A pre-start refusal shows a Russian reason and action. Its fixed provider
   wording stays available in a collapsed quote. Raw diagnostics stay
   excluded, as before.

`notice_copy.py` holds the pre-start refusal and limit copy; other notices
keep their builders. `tests.test_notice_copy` enforces the language, length,
no-replay and collapse rules.

## Consequences

- No schema, state transition, delivery semantics or retry authority
  changes. The `retry` Reply and its bindings are unchanged; only its
  explanation is translated.
- Unattempted delayed snapshots use the existing `available_at` lease
  filter. Attempted or unknown deliveries keep their evidence.
- A quick job now produces no Hub lifecycle message. Telegram typing actions,
  the provider's own progress messages and the quiet warnings remain the
  activity signals.
- Wording is a product contract, but tests assert on short phrases rather
  than whole sentences, so copy can be edited without touching state code.
- Telegram resolution of an uncertain job is a separate decision
  (stabilization plan step 3).
