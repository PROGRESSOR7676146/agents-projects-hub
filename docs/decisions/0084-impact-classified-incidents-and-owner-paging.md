# ADR 0084: Impact-classified incidents and owner paging

Status: accepted 2026-10-30; implementation planned in stages.
Date: 2026-10-30
Owner: repository owner
Builds on: [ADR 0048](0048-out-of-band-update-and-incident-plane.md),
[ADR 0036](0036-durable-local-root-blockers.md),
[ADR 0020](0020-immutable-indeterminate-resolutions.md),
[ADR 0079](0079-long-human-permission-waits.md),
[ADR 0072](0072-trusted-personal-installation.md).

## Context

The owner wants the Hub to watch its services and the provider sessions inside
them and, when something stops his work, to call a "service engineer" who
diagnoses and fixes it while he is away from the machine. One installation
produced the following evidence over three days:

1. **A silent blocker.** A Claude turn waited about 14 hours for a human file
   permission. The permission was revoked, and the outcome stayed unresolved.
   The canonical-root admission gate (ADR 0030, ADR 0036) then correctly refused
   every new productive request in that project for about 26 hours. The monitor
   also detected the condition correctly. Its edge-triggered warning
   `unresolved_provider_outcome` reached the Operations topic once and stayed
   open until an operator-led session audited the job, recorded an immutable
   resolution (ADR 0020) and confirmed recovery with a Telegram turn.
2. **Noise.** The incident journal reached its 500-episode bound in three days.
   Most episodes were flapping or sticky warnings:
   - recovery-channel checks that flap for five minutes every few hours;
   - a worker slot reported `degraded` for more than 11 hours after a single
     pre-execution refusal, while that slot and the others kept serving turns.

   The one actionable episode could not be told apart from this background.
3. **An ad-hoc watcher.** A shell loop outside the product checked units,
   revision convergence, stale components and executing heartbeats, and woke an
   operator coding-agent session. It stayed quiet for ten hours, yet it never
   read unresolved outcomes, so it would have missed the blocker above. Quiet
   hours do not prove coverage.
4. **What the summoned agent did.** It ran read-only diagnosis and an
   owner-delegated indeterminate resolution. It applied `/new` after a
   session-mode refusal. Before restarting units, it asked busy sessions to
   pause through their topics. Most of these steps are owner decisions under
   the current rules.

Diagnosis: the failure was prioritization and owner notification, not missing
detection. Health semantics also matter here: a sticky request-level refusal
was published as a component state. The evidence does not show that an
autonomous responder was missing. It shows that interactive help was useful
once the owner asked for it. The night's journald logs were not retained, so
the stalled heartbeat seen in the same window still has no root cause.

## Decision

The Hub keeps one passive evaluator and the existing presentation planes. It
learns to tell work-stopping incidents from noise and pages the owner only for
them. A model never acts on its own.

### 1. Impact classes

Each episode carries an impact class, evaluated every cycle from current
evidence:

- `blocking`: requested or accepted work is stopped or will not progress.
- `awaiting_owner`: progress waits for an owner decision that the system asked
  for on purpose, such as a hold, a human permission, or a delivery disposition.
  This is not a fault.
- `degraded`: capacity is reduced, but every demanded capability still has a
  healthy path.
- `diagnostic`: everything else.

The runbook catalogue gives every alert code a default class. The evaluator
derives the actual class from the affected capability and the demand behind it.
A degraded slot counts as `blocking` when work waits for the capability that
only that slot provides. Severity does not set the class.

The evaluator adds the following session-level conditions:

| Condition | Class |
| --- | --- |
| A canonical root has refused or held new admission because of an unresolved outcome for more than 5 minutes | `blocking` |
| A verified human permission wait is older than 15 minutes | `awaiting_owner`, with one notice |
| An intentional owner hold | `awaiting_owner`; its age alone never pages |

None of these changes ADR 0079 windows. They also never trigger a timeout, a
denial, a replay, or a restart. The existing detectors of REQ-OPS-004 are
reused, not duplicated. These are the expired lease, queued work over 15
minutes, committed delivery over 5 minutes, and unknown delivery.

### 2. Health semantics at the source

Components publish liveness, readiness and the last request refusal as separate
facts.

- A single pre-execution refusal is request evidence. It is not a component
  state.
- Readiness recovers only with fresh evidence for the condition that failed. A
  heartbeat or elapsed time does not count.
- When a request-level warning expires, the readiness becomes `unknown`, not
  `healthy`.

### 3. Flap control and retention

- Presentation re-arms only after 5 minutes of recovery. Underlying state and
  evidence stay visible all the time.
- A cross-episode rate limit stops one recurring condition from opening a new
  page every few hours.
- When the journal overflows, open `blocking` and `awaiting_owner` episodes are
  kept first. Omission counts are reported per class.
- An omitted actionable episode is itself an actionable condition. Saturation
  fails visibly.

### 4. Owner paging

The REQ-OPS-014 notices increment (ADR 0048 section 2) is extended. No new
card, cursor or dispatcher is added.

- A private card is sent at the first transition of an episode into `blocking`
  or `awaiting_owner`. An escalation within an existing episode also counts as a
  transition.
- `degraded` episodes appear in one daily digest derived from the journal.
  `diagnostic` episodes are journal-only.
- Storm limit: at most 3 individual cards in 10 minutes. The remaining
  never-attempted notices are coalesced into one summary card.
- Each card carries the deterministic evidence that the Hub already has.
  Examples are the episode age and the affected capability. For a root blocker,
  it also carries the bounded, read-only audit summary of the exact job.
  Assembling this evidence calls no model.
- One reminder after 2 hours, sent only when:
  - the original card has a positive delivery receipt;
  - the condition is freshly observed;
  - the owner has not dismissed the episode.

  The reminder has its own durable send fence. Unknown delivery never permits a
  reminder or a resend.
- The Operations topic of REQ-OPS-006 stays edge-triggered and unchanged.

### 5. Project-scoped blocker notices

When a blocker belongs to one project, only the Hub resolves the project
binding, and it uses the existing ADR 0036 outbox. The notice is one neutral,
exact-scope message in the owner topic of that project, deduplicated against
the exact-job notices that already exist. The incident journal stays anonymous
(REQ-OPS-013). There is no generic fan-out of operational alerts to project
groups. Audit details and controls stay owner-only.

### 6. The service engineer

The service engineer combines the parts below. The Hub gets no autonomous
agent.

- **Deterministic evidence** on the card (section 4).
- **Analyze («Разобрать»).** Only a fresh owner press, bound to the current
  episode, may start one Hermes analysis turn (REQ-OPS-014).
  - The turn uses bounded, sanitized, read-only diagnostic adapters, with
    mutation denied outside the model. It gets no general shell or admin
    access.
  - It ends with options, consequences and a recommendation.
  - One analysis may be active at a time, with a visible spend bound per turn.
- **Typed owner controls.** A control may execute only an action that its own
  contract already permits. A control that releases a root blocker after an
  audit needs an accepted ADR 0020 amendment. That amendment must bind:
  - the exact terminal job and its generation;
  - the writer and control custody checks;
  - the immutable classification;
  - the stated consequences;
  - atomic validation.

  No root diff, elapsed time, or model recommendation proves that execution had
  no effect.
- **Deterministic repair.** The existing installation-authorized monitor repair
  (ADR 0072) is unchanged.
  - Any new repair action, for example a restart of an idle worker, needs one
    intervention authority.
  - That authority serializes per target and binds to a generation. It blocks
    admission while the repair runs and checks provider and control custody
    immediately beforehand.
  - Unknown state refuses repair.
  - Version, link or pin changes stay with their separately authorized
    procedures.

Runbook entries are guidance, never executable authority.

**Owner decision (2026-10-30): the service engineer stays interactive.** An incident, timer, reminder, standing mandate, or
repair failure does not start a model turn. Principle 4, maintenance rule 8 and
AC-NF-005 stay as they are. Automatic analysis is reconsidered only after
stage 2 measures the following:

- precision of actionable pages;
- confirmed delivery latency;
- owner action latency;
- whether interactive analysis shortened resolution without needing another
  owner decision.

## Stages

1. **Hub evaluator.** Impact classes, the session-level conditions, the
   health-semantics correction, flap control and blocker-first retention.
   Offline tests only.
2. **Paging.** Enable the notices increment with impact routing, escalation,
   one reminder and the digest, plus ADR 0036 project-scoped notices.
   Installed and Telegram acceptance is separate.
3. **Analyze entry.** The read-only adapters and the spend bound
   (REQ-OPS-014 remainder).
4. **Separate ADRs** for any new typed resolution or repair control, and for any
   later automatic-analysis amendment.

Before stage 2 is activated, the following failures must be tested offline:

- Hermes down;
- monitor down;
- journal stale;
- disk full;
- delivery unknown;
- restart during a send;
- storm.

Retain diagnostic logs for 7 days, redacted and bounded in bytes, so that the
next stall can be explained.

## Alternatives

- **Status quo (Operations topic only).** It failed in context item 1.
- **Automatic service agent on blocking incidents**, under a standing owner
  mandate and a daily budget. This was the original proposal.
  - It needs an exception to principle 4, rule 8 and AC-NF-005.
  - It spends model quota in storms and fails when the provider route is the
    incident.
  - Treating "lower-priority data" as a boundary does not stop injection from
    logs.
  - Most useful fixes still need an owner decision, so autonomy adds latency
    before the owner's tap, not resolution.

  The reviewer recommended against it on this evidence, and the owner chose
  the interactive service engineer on 2026-10-30.
- **The ad-hoc operator coding-agent watcher.** It lives only as long as a
  desktop session lives, and it is not product behavior.
- **A service agent hosted inside the Hub.** This is circular: it is down
  whenever the Hub is down.

## Consequences

The following requirement amendments follow from this decision and land with the
stage that implements them:

- REQ-OPS-004 gains impact classes and the session-level conditions.
- REQ-OPS-009 separates liveness, readiness and request refusal.
- REQ-OPS-013 gains blocker-first overflow selection and per-class omission
  counts.
- REQ-OPS-014 gains impact-filtered cards, in-episode escalation, one fenced
  reminder, the digest and the storm limit.
- ADR 0036 notices gain the 5-minute admission-blockage page.

REQ-OPS-006, principle 4, rule 8 and AC-NF-005 are unchanged. Every stage
deployment needs its own authorization.

## Review record

The design brief was reviewed adversarially by GPT Astra (high) against
ADR 0048, ADR 0036, ADR 0020, ADR 0079, ADR 0072 and REQ-OPS-004..015. The
verdict was PROCEED_WITH_CHANGES with ten findings, all reflected here. The
main ones:

- automatic invocation was cut;
- new resolution controls are gated;
- impact is derived from capability and demand;
- reminders and delivery fences are bounded;
- the anonymous journal and the ADR 0036 routing are kept.
