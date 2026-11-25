# ADR 0114: One-tap new session with the previous answer as handoff

Status: accepted policy (owner decisions, 2026-11-23 and 2026-11-25);
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
   files and earlier turns are not carried. The answer and the fixed request
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
5. **One transaction.** The press is the owner's explicit `/new` plus one
   ordinary message. Durable admission archives the session that wrote the
   answer, opens its successor with the same agent, model and effort, and admits
   the fixed request as the successor's first input, all in one SQLite
   transaction. The transaction itself rechecks that the source session is
   still active and has received no later input, so a concurrent `/new` or
   message cannot be overtaken. Any guard failure commits nothing and the button
   stays usable; only an admitted message or a durably recorded refusal spends it.
   Normal admission rules apply: a persistent root blocker refuses the message
   visibly and spends the button, as for a letter.
6. **Refusals.** The press refuses, with a plain reason, when that session is no
   longer the topic's active session (including after a manual `/new`), when
   later input already reached it, while work or result delivery is pending or
   running, and while a local writer owns it. A press that already admitted its
   message reports the first use before any other check, even with the setting
   now off, so a repeat after a crash or of another option never changes the
   choice.
7. **Request.** The fixed owner request says the owner started a new session
   with the button, labels the answer as the previous agent's notes rather than
   new owner instructions, asks to check them against the repository, and names
   the chosen option as the owner's decision.
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
issues in the first candidate; all are fixed with these tests. Release, enabling and owner live acceptance
are tracked in the [stabilization plan](../operations/STABILIZATION_PLAN.md).
