# Agents Projects Hub

Privacy-first orchestration hub connecting Telegram project topics to persistent
Codex, Hermes, and other agent sessions with context handoffs, model switching,
approvals, and terminal takeover.

> **Status:** v0.3 pilot. The Codex ↔ Hermes flow has passed live acceptance on
> a private Telegram forum. Reproducible installation, migrations, diagnostics,
> CI, and Gemini/OpenCode CLI adapters are included; additional live projects
> and provider combinations still require operator acceptance.

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
        ├── Hermes gateway/plugin ─── persistent Hermes topic session
        │
        └── Gemini/OpenCode CLI adapters ─── provider-owned sessions
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

Terminal launching is configuration-driven. Supported backends are WSL with
Windows Terminal, Linux terminal emulators, macOS Terminal, and `tmux-only` for
manual attachment. All backends create the same named tmux writer first.

## Implemented pilot

- Strict Telegram owner, private-supergroup, topic, and project allowlists.
- One persistent Codex session per topic through the Codex app-server.
- Idempotent Telegram update processing and local SQLite recovery.
- Codex metadata footer with model, reasoning effort, context, and available
  usage-window information read from structured app-server events.
- `/pilot`, normal text, `/new`, `/new all`, `/model`, `/agent`, `/terminal`, and
  `/release` flows, plus `/status` diagnostics.
- Bidirectional Codex ↔ Hermes handoff with fail-closed Hermes admission.
- Locally managed Gemini and OpenCode headless adapters with structured output,
  persistent session IDs, bounded handoffs, and no auto-approval flags.
- Explicit terminal writer takeover/release using tmux and `codex resume`.
- Versioned SQLite migrations with automatic pre-migration backups and rollback.
- Local project administration and worktree-backed parallel-lane foundations.
- User-level service templates, installer, doctor, CI, CodeQL, and Dependabot.

See [the current specification](docs/PROJECT_HUB_SPEC.ru.md) for acceptance
results and the next implementation queue. Older design documents in `docs/`
are retained for history and are marked when superseded.

## Repository layout

```text
config/       publishable configuration examples (real config is ignored)
docs/         architecture, security model, roadmap, and pilot specification
integrations/ Hermes plugin and visible-turn export hook
scripts/      installer, doctor wrapper, and complete validation gate
src/          router, adapters, state, Telegram service, and terminal runtime
systemd/      user-service and Hermes gateway drop-in templates
tests/        unit and contract-style tests with fake external services
```

## Requirements

- Python 3.11+
- `aiohttp` 3.9+
- a current Codex CLI/app-server installation for Codex sessions
- Telegram bots configured for the intended private forum
- Hermes with user-plugin and hook support for the Hermes adapter
- Gemini CLI and/or OpenCode only when those adapters are configured
- tmux plus a supported terminal backend for `/terminal`

If an active bot must receive ordinary group messages without an explicit
mention, disable its Telegram Privacy Mode and remove/re-add the bot after the
change. Keep the group private and restrict `owner_user_ids` in local config.

## Local setup

For a user-service installation, review the script and run:

```bash
./scripts/install.sh
```

It creates a private virtual environment, copies configuration examples only
when local files do not already exist, installs the Hermes plugin/hook, and
installs—but does not enable—the systemd user units. Edit the generated files,
then run `scripts/doctor.sh` before enabling any service.

For development, install in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
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
agents-projects-hub migrate /path/to/state.db
agents-projects-hub doctor config/hub.json
agents-projects-hub serve config/hub.json
```

The default service manages Codex. Additional locally managed adapters use an
instance unit, for example `agents-projects-hub@gemini.service`. Hermes remains
owned by its native gateway and uses the included drop-in. Copy the desired
objects from `config/external-agents.example.json` into the local `agents` array;
the primary example does not require unused external bot credentials.

## Operations CLI

```bash
# Read-only diagnostics and persisted status
agents-projects-hub doctor config/hub.json
agents-projects-hub status config/hub.json

# SQLite-consistent backup and versioned migration
agents-projects-hub backup /path/to/state.db
agents-projects-hub migrate /path/to/state.db

# Local-only project administration
agents-projects-hub project list config/projects.json
agents-projects-hub project add config/projects.json \
  --id my-project --name "My Project" --topic "My Project" --root /allowed/root/project
agents-projects-hub project disable config/projects.json my-project

# Separate Git worktree for a concurrent lane
agents-projects-hub lane create config/hub.json --project my-project --lane backend
agents-projects-hub lane list config/hub.json
```

Project creation remains local: Telegram cannot submit or approve filesystem
paths. Lane creation makes a sibling Git worktree and records it in state; it
does not automatically bind a Telegram topic or start an agent.

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

For response provenance, merge `config/hermes-display.example.yaml` into the
Hermes config. It enables Hermes's native runtime footer; the Hub hook does not
rewrite response bodies or inspect hidden reasoning.

## Tests and quality gate

The suite uses fake Telegram/provider transports and disposable Git/SQLite
fixtures; it does not contact Telegram, Codex, Hermes, Gemini, or OpenCode:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Run the same formatting, lint, type, test, and example-validation gate as CI:

```bash
python scripts/validate.py
```

After the first CI run, an authenticated administrator can enable private
vulnerability reporting, Dependabot security updates, and the included `main`
ruleset with:

```bash
./scripts/configure-github.sh OWNER/agents-projects-hub
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

- Run live acceptance for a second private project group.
- Validate Gemini and OpenCode with real provider accounts and bot tokens.
- Add a Hermes-native unified metadata footer upstream or through a stable hook.
- Bind locally created worktree lanes to Telegram topics with explicit operator
  confirmation and safe cleanup.
- Add additional terminal emulators only through reviewed argv-only backends.

## License

Released under the [MIT License](LICENSE). See [Acknowledgments](ACKNOWLEDGMENTS.md)
for the open-source projects and public interfaces that made this integration
possible. Agents Projects Hub is an independent project and is not endorsed by
the upstream product vendors.
