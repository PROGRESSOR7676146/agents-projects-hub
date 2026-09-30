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
| Hermes | Sends incident cards; analyzes when the owner asks; prepares update plans and offers approval controls | Approves, starts a switch or rollback from a model turn, or answers Codex/tlive approvals |
| `stack-update` | Stages, checks, switches, and rolls back from an exact plan | Picks versions itself, or runs live inference without an explicit owner flag |
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
- **Hermes-side cursor.** Hermes keeps its cursor (the episodes it has carded
  and their message IDs) in its own private state. It never writes the journal.
- **Silent Hub.** A journal not updated for three monitor intervals produces a
  card of its own. This lets Hermes notice a failure that the Hub cannot report.
- **Storm bound.** A per-window card limit collapses an alert storm into one
  summary card.
- **Operations unchanged.** The Operations topic stays the Hub-owned channel of
  REQ-OPS-006; cards do not replace it. After cards pass acceptance, the
  monitor's direct `hermes send` recovery push is retired. The owner does not
  get the same alert twice, and the Hub stops depending on the Hermes CLI.

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
| `plan` | Read-only comparison of current and candidate versions; prints a plan digest |
| `stage` | Installs a candidate into a new version directory, only from the component's configured source; verifies a published digest where one exists; changes no link and no unit |
| `check` | Offline interoperability gates on staged candidates: version, a protocol handshake without a turn (for example app-server `initialize`), and the proxy model list over loopback. A live inference smoke check runs only with an explicit owner flag |
| `switch` | Flips links in dependency order (proxy → Codex and tlive → Claude Code, Antigravity, and OpenCode), restarts the affected units, and runs health gates. A failed gate restores the previous links automatically |
| `rollback` | Restores the previous links and restarts the affected units |

The tool uses only the Python standard library and is installed as its own
pinned copy, so it works when a Hub or Hermes release is broken. `switch` and
`rollback` run as a separate transient user unit, so they survive a restart of
Hermes Gateway.

**Owner approval.** A Hermes model turn may call `plan`, `stage`, and `check`
through a plugin tool. It may also request approval, which posts an approval
card with the plan digest and the buttons «Применить» and «Отмена». Only the
deterministic button handler starts `switch`, and only after it verifies:

- the owner's identity;
- that the digest still matches the staged plan.

A model turn has no path to `switch` or `rollback`. «Откатить» works the same
way.

Hub releases keep their own immutable procedure (REQ-OPS-010, REQ-OPS-011) and
update last; `stack-update` does not replace it.

### 6. Watchdog for Hermes' own update

When a switch restarts Hermes Gateway, the transient unit arms a watchdog. If a
fresh Hermes heartbeat does not appear within a bounded time, the watchdog:

1. restores the previous Hermes version;
2. restarts the gateway;
3. records the outcome.

The Hub monitor's existing recovery-plane checks report this in Operations, so
the owner is informed even while Hermes is down.

## Stages

1. `stack-update` and the private manifest format; offline tests only.
2. Passive observation in Hub: the drift check in `doctor` and the monitor, the
   incident journal, and the runbook catalogue.
3. Hermes integration: cards, «Разобрать», the plan/stage/check tool, and the
   approval controls.
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
