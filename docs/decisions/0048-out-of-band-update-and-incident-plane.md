# ADR 0048: Out-of-band update and incident plane

Status: accepted; implementation planned in four stages
Date: 2026-09-30

## Context

Project Hub runs on a stack of separately released components: the provider
CLIs (Codex, Claude Code, Antigravity, OpenCode), tlive, a local provider proxy,
and Hermes. Each ships on its own schedule. Some install in place through a
package manager, so an update replaces the executable under running processes
and leaves no rollback artifact and no interoperability check. Recent examples:

- A Codex CLI update moved the shared app-server socket into a per-user
  temporary directory that the Hub units' `PrivateTmp` hid. The Codex worker
  silently fell back to stdio for two days, and the model catalog went stale.
- A proxy keep-alive default cut off long Antigravity review turns.
- A newly released provider model stayed unavailable until the proxy lists it.

AC-NF-007 already requires external upgrades to pass backup, contract tests,
smoke tests, health gates, and rollback, but nothing implements it. The Hub
cannot be its own updater: an update that breaks the Hub would also break the
tool meant to roll it back. Principle 8 also forbids a chain of mandatory
dependencies between Project Hub, Hermes Gateway, and tlive.

Operational alerts reach the Operations topic (REQ-OPS-006), but their text is
not persisted, and nothing proposes what to do. The owner wants an incident
message with the quoted trigger, the options, their consequences, and a
recommendation.

Maintenance rule 8, principle 4, and AC-NF-005 forbid inference from monitors,
timers, and passive observation. Automatic model analysis of every incident
would spend quota during alert storms. It would also fail exactly when the
provider quota is the incident.

## Decision

Hub observes, Hermes presents and proposes, a deterministic tool acts, and the
owner decides.

| Participant | Does | Never does |
| --- | --- | --- |
| Hub | Keeps the incident journal and reports stack drift, both passively; Operations delivery is unchanged | Changes the stack, or invokes a model to observe |
| Hermes | Sends incident cards; analyzes when the owner asks; prepares update plans and offers approval controls | Approves, stages, switches, or rolls back from a model turn, or answers Codex/tlive approvals |
| `stack-update` | Stages, checks, switches, and rolls back one exact plan | Picks versions itself, or runs live inference without an explicit owner flag |
| Owner | Decides through deterministic controls | — |

### 1. Incident journal (Hub, passive)

On every cycle, before Operations delivery, the monitor projects each evaluated
operational alert into a private incident journal, replaced atomically.

- **Episodes.** Each alert key maps to one episode. An episode holds an opaque
  ID, the alert code and severity, the bounded message that Operations receives,
  and the opened, last-seen, and resolved times. The journal records its own
  update time.
- **No identities.** An alert key that embeds an identity is represented only
  by the opaque ID. The journal holds no prompt, response, path, credential,
  account, project, topic, or Telegram identity.
- **Derived projection, not state.** Like the model-catalog cache, the journal
  is not SQLite state: it needs no schema change, and a runtime rollback
  ignores it.
- **Bounds.** Mode `0600`. It keeps open episodes and resolved episodes for 30
  days, at most 500 in total.

A runbook catalogue ships in the package. For each alert code it lists options,
their consequences, and a recommended option. A contract test fails when an
emitted alert code has no entry. An unknown code in an older journal gets a
generic entry.

### 2. Incident cards (Hermes, no inference)

The Hub plugin in Hermes Gateway polls the journal read-only. For each new
episode it sends the owner one card in the Hermes chat, containing:

- the severity and code;
- the quoted trigger;
- the start time;
- the catalogue options with their consequences, and the recommendation;
- two buttons, «Разобрать» and «Скрыть».

It reports each resolution once.

- **No model call.** Deterministic code composes and sends the card.
- **Hermes-side cursor.** The plugin keeps its cursor (the episodes it has
  carded and their message IDs) in a private file in the Hermes state
  directory, replaced atomically. It never writes the journal or Hub state for
  this. On a cold start with a missing or unreadable cursor, it marks every
  existing episode as seen and sends one summary card of the open episodes,
  never a card per historical episode.
- **Silent Hub.** A journal not updated for three monitor intervals produces a
  card of its own. This lets Hermes notice a failure that the Hub cannot report.
- **Storm bound.** A per-window card limit collapses an alert storm into one
  summary card.
- **Operations unchanged.** The Operations topic stays the Hub-owned channel of
  REQ-OPS-006; cards do not replace it.
- **Transition from `hermes send`.** Until stage 3, the monitor's existing
  `hermes send` recovery push to the Hermes chat stays as it is, and no cards
  exist. Stage 3 adds one explicit configuration switch. When it enables cards,
  the same release stops the monitor's `hermes send` push, so the owner never
  gets the same alert twice. When it is off, the old push works unchanged. After
  cards pass acceptance, the push and its `hermes_notify_target` cooldown claims
  are removed, and the Hub stops depending on the Hermes CLI.

### 3. Analysis only on explicit request

Only a press of «Разобрать» by an authorized owner starts a Hermes model turn.
That press is the explicit interactive request that rule 8 requires.

- The turn receives the card and its catalogue entry as lower-priority data,
  not instructions.
- It may run read-only diagnostics.
- It answers with options, consequences, and a recommendation.
- Callback data is opaque and bound to one episode. A stale or unknown callback
  gets a short answer and no turn.
- Any state-changing step needs a further owner action (section 5).

### 4. Stack manifest and drift (Hub, passive)

- **Private manifest.** A stack manifest outside Git lists, for each component:
  - the pinned version and its version directory;
  - the switchable `current` link;
  - the units to restart;
  - the checks;
  - its position in the update order.
- **Version directories.** Components run from immutable per-version
  directories behind that link, like Hub releases (ADR 0012). Components
  installed in place by a package manager move to this layout before
  `stack-update` manages them.
- **Drift detection.** `doctor` compares the installed and running versions
  against the manifest without network access or inference. The monitor raises
  an edge-triggered drift alert, which reaches Operations, the journal, and a
  card.

### 5. `stack-update` (deterministic tool)

| Command | Effect |
| --- | --- |
| `plan` | Read-only. Compares current and candidate versions from the manifest's configured sources and prints a plan: exact versions, sources, published digests where available, the dependency order, and a digest of the plan and the current links. It installs and executes nothing. |
| `apply PLAN` | Stages, checks, and switches one plan, in that order. Staging installs each candidate into a new version directory, only from its configured source. It verifies a published digest where one exists, and disables package lifecycle scripts unless the manifest marks a component as needing them. Checks are offline interoperability gates: version, a protocol handshake without a turn (for example app-server `initialize`), and the proxy model list over loopback. A live inference smoke check runs only with an explicit owner flag. The switch flips links in dependency order and restarts the affected units, then runs health gates. A staging or check failure stops before any link changes. A failed health gate restores the previous links automatically. |
| `rollback SWITCH` | Restores the links recorded before one completed switch and restarts the affected units |

**Scope and order.** The dependency order is: proxy → Codex and tlive → Claude
Code, Antigravity, and OpenCode → Hermes. Hermes switches last and alone, under
the watchdog of section 6. Project Hub is not a `stack-update` component. When a
stack change needs a new Hub release, for example new unit settings, that
release is deployed afterwards through its own immutable procedure
(REQ-OPS-010, REQ-OPS-011), which keeps its own rollback.

**Independence.** The tool uses only the Python standard library and is
installed as its own pinned copy, so it works when a Hub or Hermes release is
broken. `apply` and `rollback` run as a separate transient user unit, so they
survive a restart of Hermes Gateway.

**Serialization.** One exclusive lock serializes `apply` and `rollback`; a
second request while one runs is refused, not queued. Each completed switch
writes a private record of the links before and after.

**Owner approval.** A Hermes model turn may call only `plan` and read-only
status through a plugin tool. It may also request approval, which posts an
approval card showing the plan and two buttons, «Применить» and «Отмена». The
approval card:

- carries a single-use opaque token, stored by the deterministic handler with
  the plan digest and a state: pending, then started, cancelled, or expired;
- expires after a bounded time (30 minutes by default);
- expires when Hermes Gateway restarts, because a pending approval is never
  restored as granted (REQ-SEC-003).

Only the deterministic button handler starts `apply`, and only after it
verifies:

- the owner's identity;
- that the token is pending and moves it to started atomically;
- that the plan digest still matches the current links.

A second press, a press from chat history, and a press after any link change
are all refused.

Every switch result card, successful or not, offers «Откатить», bound to that
switch's record under the same single-use, expiring and restart rules. A later
rollback goes through an approval card requested explicitly. A model turn has
no path to `apply` or `rollback`.

### 6. Watchdog for Hermes' own update

When a switch restarts Hermes Gateway, the transient unit arms a watchdog. The
watchdog reads the Gateway heartbeat marker that `doctor` already checks
(REQ-OPS-005), together with the unit state. If the unit is not active, or no
heartbeat newer than the switch appears within a bounded time, the watchdog:

1. restores the previous Hermes links;
2. restarts the gateway;
3. records the outcome.

The Hub monitor's existing recovery-plane checks report this in Operations, so
the owner is informed even while Hermes is down.

## Stages

1. `stack-update` (`plan`, `apply`, `rollback`, lock, switch records) and the
   private manifest format; offline tests only.
2. Passive observation in Hub: the drift check in `doctor` and the monitor, the
   incident journal, and the runbook catalogue.
3. Hermes integration: cards, «Разобрать», the `plan` tool, the approval and
   rollback controls, and the switch away from `hermes send`.
4. The Hermes self-update watchdog.

Each stage is deployed only with the owner's authorization. Moving an
in-place-installed component into version directories on a host is a separate
authorized deployment task.

## Alternatives rejected

- **Hub as the updater:** circular. The broken component would own its own
  repair.
- **Automatic analysis of every incident** (the owner's option B): it would
  need an exception to rule 8, spend quota during alert storms, and fail when
  the quota is the incident. The owner chose explicit analysis on 2026-09-30.
- **Hermes reading the Operations Telegram topic:** it depends on the Telegram
  delivery that incidents often break, and it yields unstructured text instead
  of episodes.
- **A dedicated update agent or service:** another runtime to keep alive.
  Hermes already has an independent channel, provider route, and credentials.
- **Version pinning in the package manager alone:** no rollback artifact, no
  interoperability gate, and running processes keep executing a deleted
  binary.

## Consequences

- REQ-OPS-013 (incident journal and runbook catalogue), REQ-OPS-014 (cards and
  explicit analysis), and REQ-OPS-015 (stack manifest, drift, and coordinated
  update) are planned. AC-NF-007 gains an implementation path.
- Hermes becomes the incident interface. If Hermes is down, the Operations
  topic works as before, and the two channels stay independent.
- The Hermes plugin grows from topic admission to a poller, callback handlers,
  and a tool. It already relies on Hermes adapter internals, so the plugin
  compatibility check and Hermes version gates matter more.
- Operating cost: the private manifest must be maintained, and the components
  that are installed in place must be moved to version directories.
