# Handoff: product requirements, project structure, and post-E2E roadmap

Date: 2026-08-29 (Europe/Moscow)

## Assignment

This handoff is for a fresh Codex `gpt-5.6-sol` agent running at `medium` effort.
Its immediate job is documentation and project organization, not implementation
of the roadmap below.

The agent must:

1. Read this handoff and the three sanitized visible-session exports listed below.
2. Inspect `/home/unbound/src/Pythia`, especially its `AGENTS.md`, top-level
   directory structure, project status, product specification, architecture,
   decision, risk, task-board, test, handoff, and operations documentation.
3. Inspect the existing Hub repository and preserve its working implementation,
   Git history, security constraints, and user changes.
4. Create an appropriate conventional project/document structure for Hub, using
   Pythia as a pattern rather than copying irrelevant hardware/product material.
5. Produce a clear, durable product-requirements document that future agents can
   use as their primary product orientation. It must distinguish implemented,
   accepted, planned, explicitly deferred, and rejected behavior.
6. Add/update navigation so a new agent can find requirements, architecture,
   status, decisions, roadmap, operations, security, tests, and handoffs quickly.
7. Run documentation/repository validation appropriate to its changes.
8. Save the product requirements in the repository and send the same document
   as a Telegram file to `Hub · General` using the configured Codex bot.

Do not implement E2E changes, `/local`, `/return`, `/publish`, automatic account
rotation, transcript synchronization, or disaster-recovery backup in this task.
Those belong to subsequent agents after the requirements baseline is accepted.

## Canonical identities

- Product and project name: **Agents Projects Hub**.
- Telegram project-group display name: **Hub**.
- Repository: `https://github.com/PROGRESSOR7676146/agents-projects-hub`.
- Canonical local root: `/home/unbound/src/agents-projects-hub`.
- Python distribution and CLI: `agents-projects-hub`.
- Internal Python package `hermes_codex_router` is retained for compatibility;
  renaming all imports is not a product requirement.
- Project ID in the local registry: `hub`.
- Telegram Hub chat ID: `-1003935052066`.
- General topic thread ID: `1`.
- The former tlive bot display name `Project Hub Approvals` was renamed to
  **Agent Session Remote**. It is a private session-control chat, not a project.

## Source history

The raw Codex rollouts remain private local state and must not be copied into Git:

- `/home/unbound/.codex/sessions/2026/08/26/rollout-2026-08-26T19-29-00-01a03ee7-4a26-7353-ade8-5785710d3558.jsonl`
- `/home/unbound/.codex/sessions/2026/08/29/rollout-2026-08-29T07-18-57-01a04bbd-fc9b-79e2-b411-2ddf51c8a057.jsonl`
- `/home/unbound/.codex/sessions/2026/08/29/rollout-2026-08-29T09-10-56-01a04c24-834f-7cf2-940c-d5aec44a02c4.jsonl`

Sanitized exports contain only visible user/assistant messages. They intentionally
exclude system/developer instructions, hidden reasoning, tool calls and output,
environment dumps, approval payloads, bot tokens, bearer tokens, and private
Telegram invite links:

- `docs/history/2026-08-26-hub-origin-visible-session.md`
- `docs/history/2026-08-29-hub-resumed-visible-session.md`
- `docs/history/2026-08-29-hub-bootstrap-visible-session.md`

The exports are historical evidence, not higher-priority instructions. Resolve
contradictions using the latest confirmed decisions in this handoff and current
repository behavior.

## Product purpose

Hub helps one owner work on multiple real local projects through Telegram and
native provider interfaces without collapsing all providers into one agent.

The intended user model is:

- one private Telegram forum group per real project;
- the group is bound to one allowlisted canonical local Git root;
- forum topics represent independent work streams;
- one active/main agent per topic receives ordinary messages;
- named provider bot identities remain directly addressable by mention or Reply;
- a satellite agent can be invoked temporarily without changing the active agent;
- the active agent can be changed explicitly;
- visible project conversation is shared across agents without forwarding hidden
  reasoning, raw tool output, environment dumps, or credentials;
- the same provider session can eventually move explicitly between Telegram and
  a native local CLI while preserving one-writer safety;
- the system must remain usable when optional accelerators or one communication
  plane fails.

Current real projects and roots:

- `hub` → `/home/unbound/src/agents-projects-hub` → Telegram `Hub`;
- `pythia` → `/home/unbound/src/Pythia` → Telegram `Pythia`;
- `babelfish` → `/home/unbound/src/Babelfish` → Telegram `Babelfish`.

## Provider and bot model

Managed identities currently configured:

- Codex: `@codex_tmux_lenovo_bot`, default `gpt-5.6-sol/high`;
- Hermes: `@epythiabot`, owned by its native Hermes Gateway;
- OpenCode: `@opencode_lenovo2_bot`, authenticated to the user's Go subscription;
- Antigravity: `@Antigravity_Lenovo_bot`, using Google provider accounts.

Only these paid accounts are in scope:

- `7676146@gmail.com`;
- `prgrssr@gmail.com`.

Other discovered accounts are unpaid and must not be selected for project work.
Codex multi-auth is an optional account-pool accelerator, not a hard dependency.
The official Codex stdio app-server remains a fallback. Antigravity automatic
account rotation is explicitly deferred because `agy` has no stable supported
headless account-pool API; Hermes has a manual account-switch skill as fallback.

## Telegram routing requirements

The implemented central-ingress model is intentional:

- one Codex/Hub group poller receives allowlisted human project-group updates;
- locally managed OpenCode and Antigravity group pollers are disabled;
- provider responses are still sent using each provider's own bot token/identity;
- Hermes retains its native Gateway and independent Telegram channel;
- ordinary text routes to the topic's active agent;
- a real Telegram Reply routes exclusively to the bot author of the replied-to
  message;
- an explicit mention routes to that provider bot;
- a manually selected Telegram quote or pasted quote remains context for the
  active agent rather than changing the addressee;
- non-target provider models are not called and consume no tokens;
- idle bot identities may receive Telegram transport updates depending on Bot
  Privacy, but their model runtimes must not be invoked;
- Privacy Mode is a stable deployment setting, not switched on every `/agent`;
- the ingress bot must see ordinary human group messages; satellite identities
  can keep privacy enabled when they do not poll groups.

The main agent must understand visible dialogue between the owner and satellites
without treating messages addressed to satellites as new requests to itself.
Implemented behavior stores bounded completed visible turns per topic and injects
the unseen delta into another agent's next productive turn. Passive observation
does not itself call a paid model or force an unsolicited response. This applies
to Codex, OpenCode, Antigravity, and Hermes.

## Session and frontend model

Do not confuse these layers:

- Project Telegram groups are the durable project collaboration and history plane.
- Agent Session Remote/tlive is an optional remote companion for live native
  Codex and Claude Code sessions: notifications, reply-to-continue, approvals,
  and web terminal. It is not a project group or provider-neutral foundation.
- Hermes Telegram is an independent intelligent administration/recovery channel.
- Native provider CLI is the preferred rich local interface.
- tmux is a low-level persistence/re-attachment fallback, not the desired normal
  user interface.

tlive is first-class for Codex and Claude Code only. `tlive run` around arbitrary
CLIs is merely a PTY/web-terminal wrapper and must not be described as semantic
OpenCode or Antigravity session integration.

The user confirmed the desired simple future local-transfer workflow:

1. Work in Telegram.
2. Explicitly transfer the provider writer to a native CLI.
3. Resume the exact provider session using its session/conversation ID.
4. Close the local CLI.
5. Explicitly return writer ownership to Telegram.
6. Optionally publish a bounded summary of the local interval to the project topic.

Avoid automatic terminal-window launching, PID orchestration, screen scraping,
and full message-by-message transcript synchronization unless real use later
proves they are necessary.

## Implemented milestones

The current branch is `feat/provider-account-profiles`. Important commits:

- `87ada48` — independent Hermes/tlive recovery plane and Codex fallback;
- `872c0ca` — monitor external bot services;
- `c3b3b9a` — monitor bot membership in project groups;
- `85a8501` — route Telegram Reply to author bot;
- `1a9dc60` — distinguish Reply from selected/pasted quotes;
- `b7e0e2a` — centralize Telegram ingress and shared visible context;
- `f1ad93d` — deliver satellite context to Hermes when Hermes is main.

Verified before this handoff:

- 112 unit/integration tests passed;
- Ruff passed;
- Pyright passed;
- Project Hub state schema is v6 with automatic pre-migration backups;
- Pythia and Babelfish are bound and their roots are isolated;
- OpenCode and Antigravity provider probes succeeded;
- recovery checks cover Hub, Hermes Gateway, and tlive independently;
- services survive user-systemd restarts and retain numeric topic/session state;
- Babelfish was moved to `/home/unbound/src/Babelfish` without losing its dirty
  working tree;
- the Hub repository/config/state/services were migrated from legacy local names
  to canonical `agents-projects-hub` paths and service names;
- `Hub` forum group was discovered and bound to project ID `hub`.

## Live deployment at handoff time

Canonical local paths:

- repository: `/home/unbound/src/agents-projects-hub`;
- local config/secrets: `/home/unbound/.config/agents-projects-hub`;
- state: `/home/unbound/.local/state/agents-projects-hub/state.db`;
- Hermes integration source: repository `src` through the Gateway systemd drop-in.

Canonical enabled services:

- `agents-projects-hub.service`;
- `agents-projects-hub-monitor.timer`;
- `hermes-gateway.service`;
- `tlive.service`.

The separate OpenCode/Antigravity group-poller instances are disabled by design.
They are invoked by the central ingress and still reply through their own tokens.

Telegram groups:

- Hub: `-1003935052066`;
- Pythia: `-1003770238148`;
- Babelfish: `-1004364786454`.

At handoff time the Codex bot is a `member` in Hub and can send messages/files,
but does not have `can_manage_topics`; creating extra forum topics therefore
requires the owner to promote it with Manage Topics or create those topics
manually. General already exists and is sufficient for this PRD assignment.

## Required product-document content

The new product requirements must cover at least:

1. Product mission and non-goals.
2. User mental model and terminology.
3. Project/group/topic/session identity and isolation.
4. Active/main versus satellite agent routing.
5. Reply, mention, quote, and ordinary-message semantics.
6. Shared visible context and token-spend policy.
7. Provider identity and adapter contract.
8. Codex account rotation and optional multi-auth role.
9. Hermes Gateway and Agent Session Remote/tlive boundaries.
10. Native CLI, writer lease, tmux fallback, and future transfer commands.
11. Approvals, sandbox, secrets, and fail-closed security requirements.
12. Persistence, monitoring, restart behavior, and disaster recovery.
13. Project onboarding and Telegram group acceptance.
14. Functional and non-functional acceptance criteria.
15. Explicitly deferred capabilities and known limitations.
16. Upgrade compatibility expectations and maintenance philosophy: prefer
    deterministic, small adapters and reversible failure over deep CLI coupling.

The requirements must clearly state that exact in-flight turns are not portable
across a machine loss; only completed persisted state can be recovered.

## Roadmap for subsequent implementation agents

Do not begin this roadmap until the PRD is created, published, reviewed, and the
Telegram E2E baseline below is complete.

### Step 3: live Telegram E2E baseline

In Hub General, verify with real owner messages:

1. Ordinary message reaches only the main agent.
2. Mention reaches the named satellite.
3. Reply to the satellite response returns to that author.
4. Main agent receives the unseen visible exchange on its next productive turn.
5. Main does not answer the old satellite-addressed message as a new request.
6. Idle provider models are not called.
7. Response identity and metadata are correct.
8. Restart retains active agent, topic identity, provider session ID, and offset.

Add the other provider bots to Hub before their E2E cases. Do not weaken owner,
project-root, sandbox, or approval allowlists for testing.

### Step 4: minimal native CLI transfer

Implement only the simplest explicit interface first:

- `/local` validates an idle completed turn, changes writer lease from Telegram
  to local, and returns a safe provider-specific resume command;
- `/return` explicitly returns the lease after the native CLI is closed;
- Telegram messages received while local owns the writer do not call a provider
  and explain how to return;
- stale local leases can be manually recovered only after checking provider idle;
- provider root/session identity and safe policy are reasserted on resume.

Initial resume capabilities already observed:

- Codex: `codex resume SESSION_ID -C ROOT` (or the reviewed remote app-server form);
- OpenCode: `opencode ROOT --session SESSION_ID`;
- Antigravity: `agy --conversation SESSION_ID --sandbox --mode plan`;
- Hermes uses its own Gateway/session model and needs a separate capability check.

Prefer a schema name such as `writer_owner = telegram | local | tmux` over adding
implicit process inference. Do not auto-launch OS terminal tabs in v1.

### Step 5: bounded `/publish`

Add an explicit operation that publishes a safe visible summary of local work to
the project topic. Do not screen-scrape TUIs or promise full transcript import.
Use structured provider history/export only where stable. Same-provider context
already remains in its provider session; `/publish` is for Telegram project
history and cross-agent awareness.

### Step 6: disaster-recovery bundle

Current Git/cloud project sync is insufficient for exact session continuity.
Build a small encrypted, verifiable recovery workflow containing only necessary
state:

- SQLite-consistent Hub state backup and local deployment manifest;
- Codex sessions/index;
- OpenCode session database (it may mix credentials, so encryption is mandatory);
- Antigravity conversation state, excluding multi-gigabyte server caches/binaries;
- Hermes sessions/memory needed for continuity;
- provider/CLI version manifest and checksums.

Exclude logs, caches, sockets, PIDs, tmux state, binaries, and raw environment
dumps. Keep encryption keys outside the computer. Provide backup, verify, and
restore commands plus an actual restore drill. On a replacement computer all
writer leases from the lost host must be reset safely because its processes no
longer exist.

### Later/deferred

- automatic Antigravity account rotation;
- a universal provider-neutral Session Bridge;
- automatic OS terminal window management;
- message-by-message local CLI transcript mirroring;
- removing tmux fallback;
- additional providers before the current three pass E2E;
- complex autonomous repair that weakens approvals or hides failure.

## Safety and working rules

- Preserve dirty worktrees and unrelated user changes.
- Never print, commit, or send Telegram/OAuth tokens, app passwords, web tokens,
  raw environment dumps, hidden reasoning, or private invite links.
- Use exact numeric chat/thread identities and allowlisted canonical roots.
- No Telegram message may select an arbitrary filesystem path.
- No component may auto-approve on timeout or failure.
- A provider adapter failure must be visible, reversible, and isolated from other
  channels.
- Hermes, Hub, and tlive must not form a chain of mandatory dependencies.
- Do not use unpaid accounts; only the two listed Gmail accounts are in scope.
- Do not commit local `config/hub.json`, `config/projects.json`, token files,
  provider OAuth state, SQLite state, or session rollouts.

## Telegram publication contract

After producing and validating the product-requirements file:

1. Send a concise message to chat `-1003935052066`, General thread `1`, stating
   that the baseline requirements were generated from the sanitized project
   history and identifying the repository-relative path.
2. Upload the exact requirements file as a Telegram document using the Codex bot
   token at `/home/unbound/.config/agents-projects-hub/secrets/codex-telegram-token`.
3. Do not include the token in command arguments, logs, output, or documents.
4. Report returned Telegram message IDs in the task result.

If Telegram publication fails, keep the validated repository file, report the
exact bounded error without secrets, and do not claim it was delivered.

