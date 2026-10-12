# Hermes incident notices: source checkpoint and runbook

State: default-off notices/dismiss source increment; exact publication/review
evidence belongs to the PR. Owner: Hub maintainer. Verified source base: `c41f52c`.
Contract: [REQ-OPS-014](../product/PERSISTENCE_AND_RECOVERY.md), staged rationale:
[ADR 0048](../decisions/0048-out-of-band-update-and-incident-plane.md).
This slice does not complete Analyze, stack drift, updates or live acceptance.

## Prerequisites and explicit activation

Activation is a separate authorized deployment task. The Hermes plugin uses the
supported Telegram handler factory before Application initialize/start. Native
manual start/stop does not invoke post hooks; JobQueue is the supported periodic
owner. The tested seam is PTB 22.8 with APScheduler 3.11.0 and tzlocal 5.3.1.
Hermes without the optional scheduler remains supported with notices off;
opt-in refuses without JobQueue. No dependency is installed by the plugin.

Set `HERMES_PROJECT_HUB_INCIDENT_NOTICES_CONFIG` to a separate absolute private
JSON file, outside Git. Leave it absent for the existing plugin behavior.
Conspicuously fictional example (never use these identities on a real host):

```json
{
  "schema_version": 1,
  "journal": "/home/example/.local/state/agents-projects-hub/incident-journal.json",
  "cursor": "/home/example/.hermes/state/hermes-incident-cursor.json",
  "owner": 123456789,
  "interval": 60,
  "monitor_interval": 60,
  "max_cards": 3,
  "window_seconds": 300
}
```

Config, journal, cursor and lock must be private regular single-link files,
owned by the Gateway UID; config/journal are read-only to this plugin. Cursor
and lock are created as `0600`. Journal/cursor parents must exist, resolve
canonically and be private same-UID directories. Paths and sidecars may not
collide. Use one Gateway and one unchanged bot identity per cursor; concurrent
Applications or a bot replacement require a separately reviewed deployment.
Config accepts only the fields above; duplicate/unknown keys, unsafe
files and unsupported schemas refuse. The plugin does not read Hub config,
SQLite, tokens or the monitor seed. Whole-launch authority custody remains an
independent deployment gate; same-UID privacy checks do not prove it.

## Operation and failure inspection

The first cold summary suppresses historical individual cards. New episodes,
observed closures and journal silence/recovery produce deterministic text.
Silence begins after three configured monitor intervals and says health is
unknown. Journal omissions never become fabricated closures. The per-window
limit counts send attempts; deferred storm observations coalesce into one
summary. Never-attempted health edges coalesce to the latest state as well.
Summaries can report only retained/current counts and explicit journal
omissions, not complete history or delivery of every trigger.

«Скрыть» suppresses a member episode's future closure notice. It does not resolve
the incident, delete Telegram history or change Hub work. It requires the exact
numeric human owner/private chat, opaque token and positively receipted message.
The button is omitted when a notice has no retained unresolved member.
Old controls whose episodes or delivery rows are no longer retained refuse.
Own callback namespace is handled before core and never invokes a model.

Inspect only the local private cursor and bounded package diagnostics:

| Saved state | Meaning / next step |
| --- | --- |
| `pending` | Never attempted, or a proven rejection awaiting its saved deadline. |
| `attempted` / `unknown` | Send may have happened; no automatic resend. Check the owner's chat and preserve evidence. |
| `sent` | Exact private-destination receipt saved; Telegram receipt is not human-read proof. |
| `rejected` | All three attempts proved rejected; no further automatic retry. |
| Unavailable/refused | Preserve files; inspect safety/schema, destination binding, clock and capacity locally. |

The current PTB adapter treats every SDK error as unknown. PTB error classes do
not retain validated rejection-body proof and can classify contradictory HTTP
bodies as `BadRequest`. The bounded rejection branch is reserved for a transport
with independent proof and covered by synthetic engine fixtures; an SDK class
name never enables resend. This favors a missed notice over a duplicate after
an ambiguous response. Adding raw rejection proof is separate transport work.

The cursor has at most 500 seen episodes, 128 delivery records and 1 MiB.
Old `sent` records may be retired at either the count or byte bound, invalidating
their controls; 4 KiB is reserved for delivery-state/timestamp growth. Unknown and
exhausted rows are never automatically erased; a saturated cursor refuses new
projection without advancing seen/health state, while already saved due pending
notices still drain. Saturation emits local diagnostics, not its own Telegram
error card. Do not delete/reset evidence or replay sends as routine recovery.
Any archival/reset is an explicit local owner disposition; the next missing
cursor emits only a cold summary. Broken JSON, future clock, unsupported schema
or changed journal seed preserves the cursor and refuses; unsafe/unavailable
journal produces a bounded unknown-health notice without invented recovery.
Journal-parent outages follow that same path. An unavailable/unsafe cursor
parent refuses the cycle without retiring the timer; restoration permits later
cycles. No missing storage directory is created automatically.

Config/env changes retire the timer and controls until an explicit restart with
a reviewed binding. Reusing a cursor under changed owner/path/policy refuses;
never edit its binding to retarget evidence. Application stop joins active
callbacks; sends have a three-second budget, at most ten attempts per cycle.
Bounded file work runs off-loop and is joined on cancellation; stalled disk
I/O can delay shutdown. This is not a hard total shutdown deadline.

## Evidence and remaining acceptance

Offline fixtures cover cold start, recurrence/closure, uncertainty/restart,
commit faults, cancellation, proven-rejection deadlines/exhaustion, storms,
silence/recovery, unsafe files, seed/destination/clock changes, exact dismiss,
contention, config retirement and scoped controls. Existing Hermes admission
and Operations fallback regressions remain required. Reviewers read source;
automated tests do not establish human approval or Telegram delivery.

The optional real SDK fixture uses a fake Bot/Request and no network. It verifies
Application start/stop, active-callback join and a fresh Application with the same
cursor, matching Gateway's disconnect/reconnect lifecycle (not same-Application
restart). Each callback is positively witnessed. It also verifies core-callback
exclusion, actual SDK `sendMessage`/positive receipt and exactly one Request call
for a contradictory HTTP400, which remains unknown:

```bash
HUB_REQUIRE_HERMES_NOTICE_SDK=1 PYTHONPATH=src \
  /home/example/hermes/venv/bin/python -m unittest tests.test_hermes_incident_sdk
```

Run it in an isolated environment containing the tested SDK/scheduler; do not
install live dependencies to run an offline check. Missing optional dependencies
skip ordinary canonical tests; the explicit required flag turns absence into a
failure. Final exact-source review, canonical publication and hosted checks are
recorded in the PR, not deployment evidence.

Next trigger: after source review, an owner-authorized activation with supported
Gateway/dependencies and custody review. Live acceptance must prove exact owner
destination, actual cards/dismiss, journal outage, storm bound, no inference,
shutdown/reconnect and unchanged same-Operations-topic fallback. Analyze needs
a separate native-turn/authorization witness before it can be offered. Update
controls, drift, off-machine restore and release remain open. Closure: this
notices-only checkpoint may close after publication; full REQ-OPS-014 stays open.
