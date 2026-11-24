# ADR 0112: Agent mail between configured topics

Status: accepted policy (owner decisions, 2026-11-23); implemented in source.
Release, installation, enabling and live Telegram acceptance are separate.
Date: 2026-11-23.
Amends: [principle 1](../product/PRODUCT_REQUIREMENTS.md#2-product-principles),
[REQ-CTX-002 and retired REQ-COLLAB-002](../product/IDENTITY_AND_INTERACTION.md);
adds REQ-CTX-008. Supersedes the cross-topic part of the research note
[agent message and file visibility](../research/AGENT_MESSAGE_FILE_VISIBILITY.ru.md).

## Context

An agent working on one project prepared a multi-page task for the agent of
another project. The owner had to download the file from one group and upload
it into the other. Different projects live in different Telegram groups, and
Hub admits no bot-authored input, so a relay between agents must be internal
to Hub, with Telegram only as the visible display.

An independent architecture consultation (GPT Astra) recommended an inbox:
addressed delivery, owner-visible display and an owner-only processing button,
without autonomous wake. The owner chose that direction, with the first route
between two projects in both directions and the receiving topics chosen when
the feature is enabled.

Reusing existing storage was checked first. It does not fit: the result outbox
holds one message per job in the job's own topic, forwarded files are bound to
the session generation that received them, and passive forwarded quotes are
bounded to 4,000 characters and consumed per agent.

## Decision

1. **Routes.** Mail is off unless the Hub configuration lists `agent_mail`
   routes. A route is directed, has a short alias and connects two exact
   topics, each given as project ID plus numeric chat and thread ID of that
   project's group; titles never select a route. At most 16 routes. A
   statically configured project must use its configured group; a project
   bound by onboarding is checked against its current binding at runtime.
   The external sender projects the configured routes into the database at
   start, so every worker, including one that reads its own configuration
   copy, offers and checks the same routes. The key is read only by the
   Controller and sender, so `hub_deploy reconfigure` may change it.
2. **Addresses.** A turn in a topic with routes gets a bounded list of
   per-turn addresses, `<alias>-<10 hex>` derived from the job ID and the
   whole route, both endpoints included, with the target's project and topic
   title. A pasted example or an older transcript cannot match, and a route
   retargeted during the turn changes the address, so the letter is refused
   instead of reaching a recipient the agent was never offered. The suffix is
   a stale-reference guard, not authorization. Targets Hub has never seen are
   not offered.
3. **Sending.** An agent sends at most one letter per final answer: it writes
   one UTF-8 `.md` or `.txt` file of at most 64 KiB in the turn's artifact
   directory and ends the answer with the exact last line
   `[[letter: <address> | <file name>]]` outside a code fence. Hub removes the
   line and states in the answer that the letter is queued, not delivered. A
   malformed line, an unknown address, a missing or unsuitable file, or a
   letter combined with choice buttons keeps the answer and says why the
   letter was not sent. Progress text never sends a letter, and a result
   recovered after a crash keeps the line visible without sending it. The file
   is also attached to the source answer, so the owner sees what was sent.
4. **Commit.** The letter and its pending display commit inside the source
   result transaction, at most one per source job, together with the exact
   route: alias and both endpoints. If that route or an endpoint binding
   changed after the worker checked it, the letter is still kept, its display
   is recorded as rejected and the source topic is told. Every stored field is
   bounded before the commit, so a letter can never abort the source result.
5. **Display.** The Hub bot posts the letter as a document in the target topic,
   with a caption fixed at commit (sender, file, preview) and one owner-only
   «Обработать» button. Each send is saved as `sending` before the request and
   confirmed only by a receipt that carries the document, so a confirmed
   display can always be processed. Before sending, the exact route must still
   be configured and the topic must still belong to the letter's project;
   otherwise the display is rejected. Removing a route or disabling mail
   therefore stops letters not yet shown, while settlement, stale-lease
   recovery and notices continue with no route configured. A rate limit, a
   401 or a proven not-sent attempt retries, at most five attempts; another
   4xx is final; an attempted send without a processable receipt is `unknown`
   and is never repeated. A rejected or unknown display produces one notice in
   the source topic, sent only while that topic still belongs to the source
   project. Mail ranks after final results, notices, progress and session
   markers, and before the topic panel.
6. **Processing.** Only the configured owner may press «Обработать», on the
   exact confirmed Hub-bot display in that numeric topic, while the group and
   topic still belong to the letter's target project. The press becomes the
   owner's ordinary message in that topic: a fixed request with the letter
   attached as a document, admitted once per displayed letter through normal
   ingress and the ordinary material pipeline. It reaches the topic's active
   agent at press time, so a letter survives `/new` and a provider switch, and
   routing, FIFO, writers, stops, holds, root checks and approvals are
   unchanged. The fixed request labels the letter as agent correspondence, not
   an owner instruction, and asks not to copy the other project's private data
   into public files. The button shows whether ordinary ingress queued the
   message or refused it.
7. **Nothing else.** A letter never starts, steers or joins a turn and never
   reaches an agent passively. There is no wake, required reply, conversation
   tracking or agreement detection. Hub transports letters; it does not assign
   work, so [ADR 0065](0065-retire-hub-lead-advisor.md) stays intact.
8. **State.** Schema 61 adds the route projection, immutable letters and
   retained delivery evidence. Letters live in the database, not in the
   artifact spool. The migration is forward-only: a rollback needs a
   schema-61-compatible release with mail off.

## Alternatives

- *Passive injection* of the letter into the target agent's next turn
  (consultation design): rejected for this slice. The agent could act on mail
  the owner never saw, Process and passive delivery would need double-
  consumption rules, and passive context is bounded below a multi-page task.
- *Autonomous wake* of the target agent: rejected by the owner. Permission to
  disclose would become permission to spend tokens and change files.
- *Tool or MCP send*, *provider-native messaging* and *Hermes as relay*: not
  uniform across runtimes and profiles, or model-driven.

## Consequences

- Text-only runtimes without file tools cannot send letters.
- A letter is visible to everyone in both groups and is stored in the state
  database, at most 64 KiB each.
- Prompt injection by the other project's agent is the accepted
  trusted-installation risk ([ADR 0072](0072-trusted-personal-installation.md)):
  the press authorizes one ordinary turn, not the letter's proposed actions.
- Processing needs Telegram to return the displayed document to the Hub bot;
  a failed download follows the existing visible material refusal.
- The button is spent once the press is handled, even when ordinary ingress
  refuses the message, for example while a local writer owns the root. The
  button then says so, the refusal is shown as for any message, and the owner
  can forward the document afterwards.

## Verification and rollout

`tests/test_agent_mail.py` covers the grammar, configuration refusals, per-turn
addresses bound to the whole route, preparation refusals, a route retargeted
during the turn, unusual titles that still commit the source result, commit
with the result, a route removed before commit or before display, display
evidence (receipt, incomplete receipt, rate limit, rejection, ambiguity,
expired lease, unbound target or source), exact display lookup after a group
rebinding, a handled but refused press, and one end-to-end path through
the Controller, a worker and the sender: letter, display, a stranger's press
ignored, one owner press admitted once, and the target turn reading the full
letter. Installation, enabling and live Telegram acceptance are tracked in the
[stabilization plan](../operations/STABILIZATION_PLAN.md).
