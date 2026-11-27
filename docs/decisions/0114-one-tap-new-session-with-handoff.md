# ADR 0114: One-tap new session with the previous answer as handoff

Status: accepted policy (owner decisions, 2026-11-23, 2026-11-25 and 2026-11-27);
implemented in source. Release, enabling and live Telegram acceptance are
separate.
Date: 2026-11-25.
Adds: REQ-CTX-009. Amends: REQ-CTX-002 (wording only).

## Context

Long sessions re-read their whole conversation on every turn, which is slow,
costly and risks compaction. Agents already end a large task with a short
handoff (state, open checks, next step) and recommend `/new`. The owner then
had to send `/new`, confirm it and type or paste a start message, and the
choice buttons under that last answer stopped working, because they are bound
to the old session (ADR 0097). The owner asked for one tap that opens the new
session and passes the handoff and, optionally, the chosen option into it.

The first plan carried the handoff as a separate staged file shown as a Hub-bot
document, like an agent letter (ADR 0112). That needs file tools, a new table,
display evidence and sender retries. An independent architecture consultation
(GPT Astra, high) agreed that the answer itself is the better carrier and asked
for four changes, all adopted below: rotation and first input in one
transaction, no acceptance after a manual `/new`, authority from the recorded
delivery of the control, and a default-off owner setting.

## Decision

1. **Marker.** A final answer MAY end with one exact last line `[[handoff]]` or
   `[[handoff: A | B]]` (one to six options, the label rules of `[[choices]]`)
   outside an open code fence. Being the last line, it cannot be combined with
   `[[choices]]` or a letter. A malformed line stays visible. Progress messages
   never carry it.
2. **Content.** The handoff is the saved visible answer of that job without the
   marker line, so the owner sees exactly what the new session receives. Its
   files and earlier turns are excluded. Forwarded files waiting in a model
   successor remain available for ordinary input. The answer and the fixed request
   must fit the ingress budgets: an answer longer than 16,000 characters gets a
   short notice instead of the control. If Hub appended its own notice to the
   answer, the marker is no longer the last line and no control appears, as for
   choices.
3. **Control.** Like choices, the Hub bot sends the control after every part
   and file of the answer: «Продолжить в новой сессии:» with «Новая сессия с
   handoff» and one «▶ <option>» per option. The outbox part stores the options
   and a short binding of the source job; the binding is a stale-reference
   guard, not authorization. Agent text can never form such a part: Hub
   escapes the control prefix in the answer, which still displays unchanged.
4. **Authority.** Only the configured owner's press acts, and only on the exact
   delivered control: the pressed Hub-bot message must be the one recorded
   delivery in the same numeric chat and topic, the last part of its answer,
   carrying that job's own binding, and the
   topic must still belong to the source project. Missing, pruned or ambiguous
   evidence refuses; there is no fallback. The request text and the option are
   rebuilt from the saved answer, never taken from Telegram.
5. **One transaction.** When the source is still active, the owner's press
   archives it, opens a successor with the same agent, model and effort, and
   admits the fixed request as its first input in one SQLite transaction.
   After a Codex `/model` selection, the same control admits the request as
   the first input of the successor already opened with the selected model
   and effort. Consecutive selections retain the original card while their
   successors stay empty. Hub records the origin and exact successor in the
   session replacement transaction and checks both again at admission.
6. **Refusals.** Later input to the source or any successor and manual
   `/new` make the original control stale. Agent switches invalidate the
   recorded model-selection chain. Work and retained
   result delivery must settle before the press, and Telegram must own the
   writer. The admission transaction rechecks these guards, so neither a
   concurrent session change nor another input can be overtaken. An earlier
   runtime may ignore the recorded origin, so the check also binds the
   successor's original row timestamp to detect a provider switch during
   rollback. The direct rotation keeps its existing durable root refusal.
   When reusing a model successor, a root or permission refusal leaves the
   control unused. Once admitted, a repeat reports the first use, including
   after restart, with the setting off or when pressed on another option.
7. **Request.** The fixed owner request records the press, identifies the
   answer as the previous agent's notes, asks to check them against the
   repository and names the chosen option as the owner's decision.
8. **Setting.** `session_handoff` is off by default and needs the Hub bot and
   the external queue and outbox. The Controller checks it at the press. Workers
   may read another configuration copy, so the sender projects the setting into
   the database at start; workers then add a short per-turn instruction and Hub
   creates the control only while it is on. `hub_deploy reconfigure` may change
   it. There is no schema change, so rollback is the previous release; a
   control already shown by a rolled-back or disabled installation is refused.

## Alternatives

- *Separate handoff file* (first plan): heavier for no gain on a short text
  handoff; kept as a later option if answers prove insufficient.
- *Two steps* (reset, then a synthetic message through ingress): a crash or an
  ingress refusal between them leaves the old session archived without its
  handoff, and another message could become the first input. Rejected.
- *Accept the press in a fresh successor after a manual `/new`*: database
  adjacency is not owner intent; the owner may have opened that session for
  other work. Rejected.
- *Automatic handoff without a press*: Hub would start model work on its own.
  Rejected (REQ-CTX-002, REQ-CTX-004).

## Consequences

- One tap replaces `/new`, its confirmation and a pasted start message, and the
  chosen option survives the session change.
- The previous answer may quote injected text from tools; it reaches the new
  session labelled as the previous agent's notes. That is provenance, not an
  injection defense, which matches the trusted-installation model (ADR 0072).
- Every turn carries a few more prompt tokens while the setting is on.
- Text-only runtimes can use it, since no file is involved.

## Verification and rollout

`tests/test_session_handoff.py` covers the grammar, the formatter, the control
after the answer from the Hub bot, the off setting, the size notice, a press
that opens exactly one successor with the answer as its first input, a repeat
press, a plain press, foreign and forged presses, the off setting at the press,
presses after later work or a manual `/new`, refusal after queued work and
under a local writer, the rollback of the whole transaction on a failing
admission, admission that refuses to rotate another session, a manual `/new` or
later input arriving between the press checks and the transaction, a used
button with the setting now off, a recorded control carrying another job's
binding, and agent text that tries to forge a control part. An independent
source review (GPT Astra, high) found two concurrency blockers and three major
issues in the first candidate; all are fixed with these tests. The model-selection
extension covers first input, restart, repeated selection, an input race, manual
reset, older-runtime agent switching and isolation from staged forwarded files. Release, enabling and owner live acceptance
are tracked in the [stabilization plan](../operations/STABILIZATION_PLAN.md).
