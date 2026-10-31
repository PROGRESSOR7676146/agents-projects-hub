# Passive incident journal

This source candidate implements the journal/catalogue slice of
[REQ-OPS-013](../product/PERSISTENCE_AND_RECOVERY.md) under
[ADR 0048](../decisions/0048-out-of-band-update-and-incident-plane.md#passive-journal-implementation-clarification).
Hermes cards, stack drift and controlled updates remain separate work.
No deployment or live acceptance is established here.

## Source checkpoint

Current state: bounded offline candidate, base verified source
`019633df26cd606fa391688283e90562a3cda5ce`, lane `feat/passive-incident-journal`.
Owner: Hub maintainer, sole implementation writer. Astra reviewed the design;
exact-source independent review and publication evidence belong to the owning
PR. Next trigger: owner integration after those gates, then separately approved
live acceptance and remaining ADR 0048 work. Closure: source prepared; the
broader incident/update plane remains open. Inspect tracked/staged/untracked lane
state after integration before cleanup. Claude editor priority, retired Hub
roles and deferred parity remain unchanged.

## Inspection and failure procedure

After each completed alert evaluation, the monitor replaces
`incident-journal.json` beside its configured state database, including cycles
with notifications disabled or no alerts. Projection precedes notification
claims and transport. Inspect the private JSON locally:

```bash
python -m json.tool /home/example/.local/state/agents-projects-hub/incident-journal.json
```

Do not attach this file to a project conversation or commit it. Its bounded
episode messages are the evaluated Operations triggers, not delivery receipts.
An open episode's latest observation and a resolved episode's last observation
remain distinct from provider results, Telegram success or permission decisions.
The catalogue in `hermes_codex_router.incident_runbooks` supplies fixed options,
consequences and a recommended option; unknown historical codes use generic
guidance. Guidance contains no executable control payload.

The monitor result reports `incident_journal.status`, retained open/resolved
counts and omission counts. `updated` means projection succeeded. It does not
prove Operations delivery. An old journal timestamp can reflect incomplete
evaluation as well as failed publication; use cached monitor health to distinguish
those cases. A busy concurrent cycle returns `monitor_busy` without evaluating
alerts or overwriting runtime health.

The derived `incident-journal.seed` and `incident-journal.lock` live beside the
journal. All three files are private `0600`; the lock is persistent and must not
be removed while a monitor can run. Preserve the seed with the journal to keep
episode correlation. A versioned HMAC header binds the journal to its seed;
same-length seed corruption refuses reconciliation before any episode changes.
This verifier is not approval authority. Neither sidecar grants provider, update or approval power,
and runtime rollback ignores their format. Losing the journal loses its history;
it is not an authoritative task or delivery store.

Unreadable, malformed, unsupported, permission-unsafe, linked, special or
oversized inputs fail with `incident_journal_unavailable`. Missing/corrupt seed
with existing history also refuses continuation. The monitor preserves prior
bytes where publication has not replaced them, reports the failure, degrades
its health and still attempts independent Operations delivery. There is no
automatic deletion or reinitialization of invalid history. Preserve all sidecars
for an explicit local inspection/recovery task. Do not infer that old episodes
resolved from unavailable, missing or omitted data.

Initial seed creation can leave a short file after a crash, even before the
first journal exists. It refuses continuation and requires explicit local
inspection; it is never silently replaced. A backward clock step also refuses
projection until current time reaches the saved update time. Crash-left
`.incident-*.tmp` files are private abandoned outputs, not recovery evidence;
automatic cleanup does not run. Inspect them only with monitor activity stopped.
If the lock itself is unavailable or unsafe, projection is disabled and the
existing monitor/Operations path continues without journal serialization,
reporting degraded health. Lock contention instead skips the entire cycle.

At most 500 episodes are retained. Current open keys have priority, ordered by
impact class first (`blocking`, `awaiting_owner`, `degraded`, `diagnostic`;
[ADR 0084](../decisions/0084-impact-classified-incidents-and-owner-paging.md))
and then in stable opaque order; remaining capacity retains the newest resolved
rows, including the exact 30-day boundary. Open episodes do not age out.
Omission counts describe this projection cycle, not a lifetime loss count;
`omitted_open_by_impact` splits the open omissions by class. If an actionable
episode had to be omitted, the monitor reports `incident_journal_saturated`
health instead of success.

Schema 2 adds `impact` to every episode and `omitted_open_by_impact` to the
header. A schema 1 journal is read with catalogue-default impacts and rewritten
as schema 2 on the next cycle. A reader that only accepts schema 1, such as a
Hermes plugin pinned to an older Hub source, reports the journal unavailable
until it is updated. An evicted ongoing episode
that later re-enters has a new observation identity; no missing recovery event
is invented.

Focused offline checks:

```bash
python scripts/validate.py --profile focused \
  tests.test_incident_journal tests.test_incident_monitor \
  tests.test_alerts tests.test_monitor_health tests.test_reliability_alerts
```

The fixtures cover reopen continuity, recovery/rearm, exact messages without
identities, catalogue completeness, retention/storm bounds, lock contention,
unsafe inputs, failed atomic replacement and notification failure isolation.
They use temporary state and mocked transport/metadata; no inference, credentials,
installed services or deployment state is needed. Full canonical publication and
independent review remain mandatory. Later Hermes integration needs separately
authorized model-free card/cursor and silent-Hub acceptance.
