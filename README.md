# Agents Projects Hub

Privacy-first orchestration hub connecting Telegram project topics to persistent
Codex, Hermes, and other agent sessions with context handoffs, model switching,
approvals, and terminal takeover.

> **Status:** working pilot. The Codex ↔ Hermes flow has passed live acceptance
> on a private Telegram forum, but installation is still operator-driven and
> some adapters and administration commands remain on the roadmap.

## Why this project exists

Long-running software projects rarely fit into one chat or one agent session.
Agents Projects Hub turns a private Telegram forum into a durable project
control plane:

- one allowlisted Telegram supergroup maps to one local project;
- every numeric forum topic gets its own persistent provider sessions;
- ordinary messages go only to the agent currently active in that topic;
- switching agents or models carries forward a bounded, visible context handoff;
- the same Codex thread can move safely between Telegram and a local terminal;
- local configuration, sandboxing, and human approvals remain authoritative.

The hub is deliberately a deterministic router, not another autonomous agent.
Telegram can select a pre-registered `project_id`, but it cannot choose a local
filesystem path, weaken the sandbox, or approve an action on the user's behalf.

## Goals

- Keep project conversations and agent sessions persistent across restarts.
- Isolate projects and forum topics using immutable numeric identities.
- Support multiple agent runtimes without making them share credentials or
  compete for the same incoming message.
- Transfer useful context without forwarding hidden reasoning, environment
  dumps, terminal buffers, or secrets.
- Preserve Codex's `workspace-write` sandbox and `on-request` approval policy.
- Make local interactive takeover explicit and race-free.

## How it works

```text
Telegram forum topic
        │  chat_id + message_thread_id
        ▼
Agents Projects Hub ─── local registry + SQLite state
        │
        ├── Codex app-server ─── persistent Codex thread ─── project worktree
        │
        └── Hermes gateway/plugin ─── persistent Hermes topic session
```

Topic identity is the numeric pair `(chat_id, message_thread_id)`; topic titles
are display metadata and may be renamed safely. Project roots come exclusively
from a local allowlist. Runtime state—including topic bindings, provider session
IDs, writer leases, deduplication receipts, and bounded handoffs—is stored in a
private SQLite database.

### Routing and handoff

Each topic has one active agent. Normal text is admitted only by that agent;
explicit mentions can address another runtime without silently changing the
active route. `/agent` changes the active runtime and creates a provider session
with a one-time handoff:

- Codex → Hermes uses a bounded summary;
- Hermes → Codex uses bounded excerpts of visible user/assistant turns;
- hidden reasoning, tool output, credentials, and raw terminal content are
  excluded.

The Hermes integration is a native gateway plugin and turn-export hook. Hermes
continues to own its Telegram token and topic sessions, so the hub does not run
a second poller for the same bot.

### Terminal takeover

A Codex thread accepts only one active writer. `/terminal` therefore transfers a
writer lease from the Telegram bridge to a named tmux session and opens the same
thread with `codex resume`. While the terminal owns the lease, Telegram cannot
start another turn in that thread. `/release` closes the tmux session, reloads
the persisted thread, and returns the lease to Telegram.

The current terminal launcher targets WSL with Windows Terminal. The core router
is otherwise platform-neutral, but other terminal frontends need their own
launcher adapter.

## Implemented pilot

- Strict Telegram owner, private-supergroup, topic, and project allowlists.
- One persistent Codex session per topic through the Codex app-server.
- Idempotent Telegram update processing and local SQLite recovery.
- Codex metadata footer with model, reasoning effort, context, and available
  usage-window information read from structured app-server events.
- `/pilot`, normal text, `/new`, `/new all`, `/model`, `/agent`, `/terminal`, and
  `/release` flows.
- Bidirectional Codex ↔ Hermes handoff with fail-closed Hermes admission.
- Explicit terminal writer takeover/release using tmux and `codex resume`.
- User-level service operation for the deployed pilot.

See [the current specification](docs/PROJECT_HUB_SPEC.ru.md) for acceptance
results and the next implementation queue. Older design documents in `docs/`
are retained for history and are marked when superseded.

## Repository layout

```text
config/       publishable configuration examples (real config is ignored)
docs/         architecture, security model, roadmap, and pilot specification
integrations/ Hermes plugin and visible-turn export hook
src/          router, adapters, state, Telegram service, and terminal runtime
tests/        unit and contract-style tests with fake external services
```

## Requirements

- Python 3.11+
- `aiohttp` 3.9+
- a current Codex CLI/app-server installation for Codex sessions
- Telegram bots configured for the intended private forum
- Hermes with user-plugin and hook support for the Hermes adapter
- tmux, WSL, and Windows Terminal for the current `/terminal` implementation

If an active bot must receive ordinary group messages without an explicit
mention, disable its Telegram Privacy Mode and remove/re-add the bot after the
change. Keep the group private and restrict `owner_user_ids` in local config.

## Local setup

Clone the repository and install it in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Create local configuration from the examples:

```bash
cp config/projects.example.json config/projects.json
cp config/hub.example.json config/hub.json
```

Then edit the ignored local files:

1. Set `allowed_roots` and register projects by stable `project_id`.
2. Set the exact Telegram owner IDs and private supergroup IDs.
3. Point `codex_socket_path` at the local Codex app-server socket.
4. Store bot tokens in separate mode-`0600` files and reference them through
   `token_file`; inline tokens are rejected.
5. Keep the state database in a private local state directory.

Validate before starting the service:

```bash
agents-projects-hub validate config/projects.json
agents-projects-hub validate-hub config/hub.json
agents-projects-hub serve config/hub.json
```

For a systemd deployment, run the same `serve` command from an unprivileged user
unit and provide the Hermes integration environment variables there. Service
unit templates and an automated installer are not yet included, so review paths
and permissions explicitly for each machine.

## Hermes integration

The publishable integration sources live in:

- `integrations/hermes-project-hub/` — admission and handoff plugin;
- `integrations/hermes-project-hub-hook/` — bounded visible-turn exporter.

Install them using the Hermes user-plugin/hook mechanism, and provide:

- `HERMES_PROJECT_HUB_SOURCE` — absolute path to this repository's `src`;
- `HERMES_PROJECT_HUB_STATE` — the same SQLite state path used by the hub;
- `HERMES_PROJECT_HUB_OWNER_IDS` — comma-separated allowed Telegram user IDs.

Admission fails closed when the database or binding is unavailable. Explicit
Hermes mentions remain on Hermes's native exclusive-mention path.

## Tests

The suite does not contact Telegram, Codex, or Hermes:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Configuration templates can be schema-checked without creating their example
paths:

```bash
PYTHONPATH=src python3 -m hermes_codex_router.cli \
  validate config/projects.example.json --allow-missing
```

## Security invariants

- Telegram input never supplies a filesystem path.
- Canonical project roots must be inside configured allowlisted roots.
- Subprocesses are constructed as argument arrays, never shell strings.
- `danger-full-access` and automatic approvals are rejected.
- Hermes and the hub cannot approve Codex actions; Codex/tlive retain approval
  ownership.
- One writer lease protects each Codex topic session.
- Duplicate Telegram updates are processed idempotently.
- Tokens, local config, state databases, sockets, logs, and runtime files are
  excluded from Git.

Read the complete [security model](docs/SECURITY.ru.md) before deploying the hub
outside an isolated pilot.

## Roadmap

- Finish operator-friendly bootstrap, service templates, and health checks.
- Add unified metadata for Hermes responses.
- Add Gemini and OpenCode adapters behind the same admission contract.
- Support locally approved project/topic creation and renaming workflows.
- Add worktree-backed parallel lanes without concurrent writers in one worktree.
- Expand live acceptance beyond the current private Pythia pilot.

## License

No open-source license has been selected yet. Until one is added, the repository
is source-available for inspection but no additional reuse rights are granted.
