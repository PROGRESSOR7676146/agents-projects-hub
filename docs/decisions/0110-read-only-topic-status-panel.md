# ADR 0110: Read-only topic status panel as the first delivery

Status: accepted policy; not implemented. Source implementation, installation
and live acceptance are separate. The feature ships default-off.
Date: 2026-11-20.
Amends: the first-delivery scope of the
[topic panel plan](../operations/TOPIC_PANEL_IMPLEMENTATION_PLAN.ru.md).

## Context

The owner needs to see at a glance, in each project topic, whether work is
fine, whether there is recent activity and when to intervene. On 2026-11-18 the
owner declined forwarding reasoning summaries and a separate silence ping; a
per-topic status message (the owner calls it an "informer", the interim
substitute for Telegram widgets in groups) is the chosen surface.

The 2026-11-15 plan specifies a full panel in its first delivery: overview,
queue and details pages, refresh, re-post, pin and a topic Stop with one-shot
server-side tickets. Most of its complexity and risk lies in mutating
callbacks: ticket binding, stale and repeated presses, Stop races against new
work, and pin rights. None of that is needed to answer the owner's question.

An independent GPT Astra consult (2026-11-20) supported a read-only first
delivery and added conditions on activity semantics, operation evidence and
send cadence. The owner chose read-only plus one «Обновить» button.

## Decision

1. The first delivery is a read-only panel ([REQ-UX-010](../product/IDENTITY_AND_INTERACTION.md)):
   `/panel` creates or replaces one Hub-bot status message in a registered
   project topic, `/panel off` stops updates, and the only control is an
   owner-only read-only «Обновить» callback. Stop stays the existing `/stop`
   command. Queue/details pages, a panel Stop, pin and the model selector are
   later, separately reviewed deliveries; the plan stays open for them.
2. The snapshot uses only durable Hub evidence and no model calls. The
   headline follows a fixed priority: a durable blocker that needs the owner
   (indeterminate job awaiting decision, unknown delivery that holds the topic,
   approval wait), then stop requested, local terminal writer, executing or
   queued, then idle.
3. Activity is reported by kind, never as one merged timestamp. Exact-bound
   agent activity (Codex accepted-turn events, Claude stream events), delivery
   of an interim message and process observation are different facts; process
   liveness never resets silence. Silence is stated as an observation
   ("Новых наблюдаемых событий нет N мин. Это не означает остановку.") and is
   never by itself a reason to intervene. The initial highlight threshold is
   ten minutes; it is a display threshold, not an SLA or a diagnosis.
4. Panel operations are a separate, low-priority Telegram effect owned by the
   existing external sender. Each create or edit keeps its own evidence
   (instance, target message, sent payload digest, attempt, receipt or unknown)
   in panel-owned storage, apart from the provider outbox and its overdue
   alerts. Unknown creates and edits are never retried automatically; generation
   fencing keeps a late receipt from reviving a replaced or disabled panel.
   The panel is edited only when its displayed content changes, with no
   per-minute clock.
5. Panel-only failures are isolated: they never change jobs, queues, holds or
   provider health and never raise provider-delivery overdue alerts. A genuine
   shared Telegram transport failure observed by a panel operation still
   follows the existing sender-health rules.
6. The panel is visible to the whole group, so it shows only allowed status
   fields and never prompts, tool arguments, paths, raw errors or approval text.

## Consequences

- The owner gets the at-a-glance answer without a new mutation surface. Stop
  needs the existing command until a reviewed panel Stop exists.
- One additive table is needed. Its migration follows the current schema chain
  of the moment and requires a compatible rollback release, like every other
  schema change.
- With a stalled sender the panel can be stale; its "last change" time makes
  that visible. An unknown edit may or may not have applied; the panel keeps
  the last confirmed payload separately and claims neither version as shown,
  which is also why it never claims "all fine" as a guarantee.
- A repeated `/panel` after an unknown create may leave two visible panel
  messages; only the newest instance is current.

## Verification and rollout

Offline tests cover every headline state and activity kind, stale and foreign
«Обновить» presses, replacement and late receipts, unknown create/edit,
cadence below mandatory deliveries, isolation from health and overdue alerts,
and the default-off flag. Enabling the flag and live acceptance in a real topic
are owner-reserved steps.
