# Agents Projects Hub — product requirements baseline

Status: accepted baseline  
Version: 1.0  
Date: 2026-08-30
Product owner: repository owner

This document is the primary product orientation for future agents. It records
accepted intent and distinguishes current, planned, deferred, and rejected
behavior. Current implementation claims are bounded by code and passing tests;
the live Telegram E2E baseline remains a separate acceptance step.

Normative words MUST, SHOULD, and MAY are used in their BCP 14 sense.

## 1. Mission

Agents Projects Hub helps one owner work on multiple real local projects through
private Telegram project groups and native provider interfaces without
collapsing independent providers into one agent or weakening local safety.

The product binds durable project conversation to allowlisted local Git roots,
routes each topic to the intended provider session, preserves visible context
across providers, and keeps control of files, credentials, approvals, and writer
ownership on the local machine.

The product succeeds when the owner can understand which project, topic, agent,
provider session, account mode, and writer are active; communicate directly
with different agent identities; recover completed work after ordinary service
failure; and observe failures without hidden cross-project access or surprise
model spend.

## 2. Product principles

1. **Project first.** A Telegram project group is a durable collaboration and
   history plane for exactly one registered local project.
2. **Deterministic routing.** Local code, not another model, decides which agent
   receives a request.
3. **Provider identity remains visible.** Codex, Hermes, OpenCode, and
   Antigravity remain directly addressable identities with provider-owned
   sessions and credentials.
4. **One productive request, one provider turn by default.** Passive observation
   MUST NOT invoke paid models.
5. **One writer.** A provider session MUST NOT accept concurrent Telegram and
   local writers.
6. **Local authority.** Telegram may select only registered identities and MUST
   NOT select filesystem paths, relax a sandbox, or approve an action.
7. **Bounded visible sharing.** Cross-agent context contains visible project
   dialogue, never hidden reasoning, raw tool output, terminal screens,
   credentials, or environment dumps.
8. **Independent recovery paths.** Project Hub, Hermes Gateway, and Agent Session
   Remote/tlive MUST NOT form a chain of mandatory dependencies.
9. **Reversible failure.** Small deterministic adapters SHOULD fail visibly and
   locally. The system SHOULD prefer a clear degraded state over fragile CLI
   coupling or concealed repair.
10. **Evidence before breadth.** Existing providers and project isolation MUST
    pass acceptance before more providers or more autonomous behavior are added.

## 3. Scope and non-goals

### In scope

- Private Telegram forum groups mapped to allowlisted canonical local Git roots.
- Independent topic work streams and persistent provider sessions.
- One active/main agent plus explicitly invoked satellite agents.
- Direct provider identity, deterministic Reply/mention routing, and bounded
  shared visible context.
- Codex, Hermes, OpenCode, and Antigravity provider boundaries.
- Explicit model/session management and safe operational diagnostics.
- Optional Codex multi-account rotation with an official Codex fallback.
- Native provider CLI continuity, explicit writer ownership, and tmux as a
  low-level fallback.
- Independent Hermes and tlive service/recovery capabilities.
- Local persistence, monitoring, controlled upgrades, and disaster-recovery
  planning.

### Non-goals

- A provider-neutral autonomous super-agent that hides provider identity.
- Remote selection of arbitrary directories, credentials, commands, or policy.
- Automatic approvals, approval by Hermes, or approval on timeout/failure.
- Forwarding hidden chain-of-thought, raw terminal output, or secret material.
- Full message-by-message synchronization between Telegram and every native CLI.
- Screen scraping provider TUIs or automatic OS terminal/PID orchestration.
- Guaranteed portability of an in-flight turn after process or machine loss.
- A bot for every model or account; bot identity represents an agent runtime.
- Treating tlive as semantic integration for unsupported providers.
- New providers before the current provider set passes the required live E2E.

## 4. User mental model and terminology

| Term | Meaning |
| --- | --- |
| Project | A locally registered immutable `project_id`, display name, and canonical allowlisted Git root. |
| Project group | One private Telegram forum supergroup bound locally to one project. |
| Topic | A work stream identified by numeric `(chat_id, message_thread_id)`; its title is mutable display metadata. |
| Agent | A directly addressable runtime identity such as Codex, Hermes, OpenCode, or Antigravity. |
| Active/main agent | The default recipient of ordinary messages in one topic and the eventual observer of visible satellite dialogue. |
| Satellite agent | An agent invoked by mention or Reply without changing the active agent. |
| Provider session | The provider-owned thread/session/conversation associated with one agent in one topic. |
| Visible topic journal | Bounded completed user/agent dialogue eligible for cross-agent awareness. |
| Writer lease | Persisted authority for `telegram`, future `local`, or fallback `tmux` to write to one provider session. |
| Project Hub | The deterministic router/control plane and this product. |
| Hermes Gateway | Hermes's independently operated native Telegram/session runtime. |
| Agent Session Remote | The renamed tlive private session-control chat for supported native sessions; not a project group. |

The canonical product name is **Agents Projects Hub**. The Telegram project
group display name is **Hub**. The internal Python package name
`hermes_codex_router` is retained for compatibility and does not redefine the
product.

## 5. Lifecycle labels

Every capability in this baseline uses one of these labels:

- **Implemented** — present in the repository and covered by automated tests;
  live behavior may still need the explicitly listed E2E acceptance.
- **Accepted** — required product behavior or boundary, whether or not every
  provider implementation is complete.
- **Planned** — accepted next work with a defined place in the roadmap.
- **Deferred** — intentionally not scheduled until an explicit trigger or stable
  upstream capability exists.
- **Rejected** — explicitly outside the intended design unless the product owner
  revises this baseline.

## 6. Project, group, topic, and session identity

- **REQ-ID-001 (Implemented):** Telegram input MUST select only an immutable
  locally registered `project_id`; it MUST NOT supply a filesystem path.
- **REQ-ID-002 (Implemented):** Every project root MUST resolve canonically
  beneath a configured `allowed_root` and MUST be validated as the intended Git
  root before a session starts or resumes.
- **REQ-ID-003 (Implemented):** A project group MUST be identified by numeric
  `chat_id`; names and invite links are not authorization or binding.
- **REQ-ID-004 (Implemented):** A topic MUST be identified by numeric
  `(chat_id, message_thread_id)`. Renaming a topic MUST NOT change its identity.
- **REQ-ID-005 (Implemented):** Sessions, visible context, routing, and writer
  state MUST remain isolated between projects and topics.
- **REQ-ID-006 (Implemented):** Unknown, disabled, mismatched, or unbound
  identities MUST fail closed and require local confirmation; Telegram MUST NOT
  perform a filesystem rebind.
- **REQ-ID-007 (Accepted):** Each configured agent MAY maintain its own provider
  session in a topic. Switching agents MUST NOT silently merge provider-native
  session state.

Publishable examples use fictional identities only:

| Project ID | Canonical root | Telegram display |
| --- | --- | --- |
| `example-project` | `/home/example/projects/example-project` | Example Project |
| `disabled-example` | `/home/example/projects/disabled-example` | Disabled Example |

Real project names, roots, chat IDs, and topic IDs are private deployment state
and MUST NOT be recorded in this repository.

## 7. Routing and agent behavior

### Active and satellite routing

- **REQ-ROUTE-001 (Implemented):** Each topic MUST have exactly one active agent
  for ordinary human messages.
- **REQ-ROUTE-002 (Implemented):** A satellite invocation MUST NOT change the
  active agent unless the owner explicitly changes it.
- **REQ-ROUTE-003 (Implemented):** A single central group ingress MUST receive
  allowlisted human project-group updates and choose the target locally.
- **REQ-ROUTE-004 (Implemented):** Locally managed OpenCode and Antigravity
  group pollers MUST remain disabled; their own bot tokens MAY send responses so
  provider identity remains visible.
- **REQ-ROUTE-005 (Accepted):** Hermes retains its native Gateway and independent
  Telegram channel while its project-topic admission and visible-context
  exchange fail closed through the Hub integration.

### Message semantics

Routing precedence is deterministic:

1. **Real Reply — Implemented.** A Telegram Reply to a known bot-authored
   message routes exclusively to that author agent.
2. **Explicit mention — Implemented.** An explicit provider bot mention routes
   to that provider.
3. **Selected/pasted quote — Implemented.** A manually selected Telegram quote
   or pasted quotation is context for the active agent; it is not Reply
   addressing.
4. **Ordinary message — Implemented.** All other admitted text routes only to
   the topic's active agent.

- **REQ-ROUTE-006 (Implemented):** Non-target provider runtimes MUST NOT be
  invoked and MUST NOT consume model tokens.
- **REQ-ROUTE-007 (Implemented):** Transport delivery to an idle bot identity,
  if caused by Telegram Privacy Mode, MUST stop before provider invocation.
- **REQ-ROUTE-008 (Accepted):** Privacy Mode is a stable deployment setting and
  MUST NOT be toggled on agent switches. The central ingress must be able to see
  ordinary human group messages; satellite identities SHOULD retain privacy
  when they do not poll groups.

## 8. Shared visible context and spend policy

- **REQ-CTX-001 (Implemented):** Completed visible user/agent turns MUST be
  recorded in a bounded per-topic journal for active and satellite agents.
- **REQ-CTX-002 (Implemented):** On another agent's next productive turn, its
  unseen journal delta MUST be injected with explicit speaker/addressee framing.
- **REQ-CTX-003 (Implemented):** The delta MUST be acknowledged only after a
  successful productive turn, so failure does not silently lose context.
- **REQ-CTX-004 (Implemented):** Passive observation MUST NOT call a provider,
  force a response, or independently spend model tokens.
- **REQ-CTX-005 (Implemented):** Shared context MUST exclude hidden reasoning,
  raw tool output, raw terminal screens, credentials, private invite links, and
  environment dumps.
- **REQ-CTX-006 (Accepted):** The active agent SHOULD understand dialogue with
  satellites without interpreting old satellite-addressed messages as new tasks.
- **REQ-CTX-007 (Accepted):** A single user request SHOULD produce one provider
  turn unless the owner explicitly addresses multiple agents.

## 9. Provider identity and adapter contract

Every provider adapter MUST:

- use a stable agent ID and Telegram bot identity where configured;
- bind only to the canonical root selected from the local registry;
- create or resume provider sessions using structured IDs;
- construct subprocess arguments as arrays, never interpolate prompts into a
  shell command;
- preserve the configured sandbox/approval policy and reject bypass flags;
- return bounded visible output and structured session metadata;
- classify failures without exposing credentials or uncontrolled raw output;
- support an idle/busy boundary sufficient to prevent concurrent writers;
- fail visibly and in isolation so another provider/channel can remain usable.

Current provider status:

| Provider | Status | Product boundary |
| --- | --- | --- |
| Codex | Implemented | Persistent app-server thread; `workspace-write` + `on-request`; metadata and usage status. |
| Hermes | Implemented integration | Native Gateway owns Telegram/session; Hub plugin/hook owns fail-closed project admission and bounded visible exchange. |
| OpenCode | Implemented adapter | Go-authenticated provider-owned session through structured CLI output; centrally routed bot identity. |
| Antigravity | Implemented adapter | `agy` conversation in sandboxed `accept-edits` work mode; no dangerous permission bypass. |
| Gemini CLI | Rejected for active product | Google provider work uses Antigravity; do not reactivate a parallel Gemini CLI path without a new decision. |

Provider bot identity maps to a runtime, not to a model or paid account. Model
selection belongs inside the agent session and should remain visible in status
or response metadata when the provider exposes it.

## 10. Codex accounts and optional multi-auth

- **REQ-AUTH-001 (Implemented):** Only account profiles explicitly allowlisted
  by the operator are in scope. Discovered but unapproved accounts MUST NOT be
  selected for project work, and account identifiers MUST remain outside Git.
- **REQ-AUTH-002 (Implemented):** `codex-multi-auth` is an optional accelerator,
  not a Project Hub dependency.
- **REQ-AUTH-003 (Implemented):** When a healthy rotating app-server is
  configured, Hub MAY use its account pool while preserving the same Codex
  thread and exposing bounded quota/account health without tokens.
- **REQ-AUTH-004 (Implemented):** When multi-auth is unavailable, Hub MUST be
  able to use the official Codex stdio app-server rather than fail the entire
  Project Hub.
- **REQ-AUTH-005 (Implemented):** Account changes and quota state MUST remain
  visible to the owner; switching MUST NOT be silent.
- **REQ-AUTH-006 (Accepted):** Hermes MAY guide a mode-aware manual device-login
  recovery, but MUST NOT copy or display tokens, change the wrong credential
  store, or replace deterministic locking and health checks.
- **REQ-AUTH-007 (Planned acceptance):** A natural or controlled quota-exhaustion
  test must demonstrate one response, the same persisted thread, bounded retry,
  and a visible account transition.

### Compact control surface

- **REQ-CMD-001 (Implemented):** `/status` shows the active provider, model,
  effort, context remainder when observable, masked active account, and compact
  provider-supplied limit/reset windows.
- **REQ-CMD-002 (Implemented):** `/model` is the single cascaded selector for
  provider, model, and effort. It marks current values and validates callbacks
  against the exact cached catalog snapshot displayed to the user. The final
  click changes local session state deterministically and MUST NOT depend on a
  new provider RPC or an AI-generated handoff.
- **REQ-CMD-002A (Implemented):** Successful provider discovery updates a
  private atomic last-known-good catalog with source version and timestamp.
  Telegram callbacks use bounded opaque keys rather than provider model IDs;
  long catalogs are paginated. Failed discovery uses the cache and becomes an
  Operations warning only after the cached success is older than 24 hours.
- **REQ-CMD-003 (Implemented):** `/accounts` lists configured provider accounts
  and observable limits. OpenCode Go exact exhaustion/reset telemetry is shown
  only after a real provider `429`; plan caps are labelled separately.
- **REQ-CMD-004 (Implemented):** `/new` requires an owner callback confirmation
  and resets only the active provider session; mass reset behavior is removed.
  `/local` transfers writer ownership; `/return` returns ownership and
  publishes a bounded safe summary of the local interval.
- **REQ-CMD-005 (Implemented):** The public Telegram command menu contains only
  `/status`, `/model`, `/accounts`, `/new`, `/local`, and `/return`. Legacy
  maintenance commands may remain locally callable for compatibility but are
  not part of the normal mobile interface. In registered project groups only
  the central router bot publishes this universal menu; provider bots publish
  empty chat-scoped menus so Telegram does not duplicate commands with bot
  username suffixes. Direct provider chats expose only commands implemented by
  that provider endpoint. A deterministic local command checks and synchronizes
  every scope.
- **REQ-CMD-006 (Implemented):** Provider, model, and effort buttons mark the
  active choice and use Telegram's success style where supported. Account and
  quota summaries use portable green/yellow/red status symbols because message
  text itself has no reliable cross-client color API.

## 11. Frontends, writer lease, and local transfer

### Current behavior

- **REQ-WRITER-001 (Implemented):** One provider topic session MUST have only one
  active writer.
- **REQ-WRITER-002 (Implemented for Codex tmux takeover):** `/terminal` transfers
  writer ownership from Telegram to a named tmux-backed Codex CLI; `/release`
  returns it to Telegram without changing the thread.
- **REQ-WRITER-003 (Implemented):** Telegram MUST refuse productive turns while
  the terminal lease owns that session.
- **REQ-WRITER-004 (Accepted):** tmux is a persistence/reattachment fallback, not
  the preferred rich local user interface.
- **REQ-WRITER-005 (Accepted):** Agent Session Remote/tlive is first-class only
  for providers it semantically supports (currently Codex and Claude Code). A
  generic PTY wrapper MUST NOT be described as semantic OpenCode or Antigravity
  integration.

### Implemented minimal native transfer

- **REQ-WRITER-006 (Implemented):** `/local` validates that no Hub dispatch is
  running and that a completed provider session exists, changes `writer_mode`
  from `telegram` to `local`, and returns a reviewed
  provider-specific resume command for the canonical root and session ID.
- **REQ-WRITER-007 (Implemented with explicit owner assertion):** `/return`
  restores Telegram ownership after instructing the owner to close the local
  CLI and confirming that no Hub dispatch is running, then asks the same
  provider session for a bounded `Completed / Verified / Next` publication.
  Summary failure does not take the writer lease back from Telegram. V1
  deliberately does not infer OS process state.
- **REQ-WRITER-008 (Implemented):** Messages arriving while `local` owns the
  writer do not call a provider and explain how to return safely.

Initial reviewed resume shapes are `codex resume SESSION_ID -C ROOT`,
`opencode ROOT --session SESSION_ID`, and
`cd -- ROOT && agy --conversation SESSION_ID --sandbox --mode accept-edits`. They are version-sensitive
adapter capabilities, not permanent user-input templates. Hermes requires a
separate native capability check.

## 12. Approval, sandbox, and secret requirements

- **REQ-SEC-001 (Implemented):** Default Codex policy MUST be
  `workspace-write` plus `on-request`; `danger-full-access`, dangerous provider
  bypass flags, and automatic approval MUST be rejected.
- **REQ-SEC-002 (Implemented):** Hermes and Hub are not approval authorities.
  Codex/tlive retains approval ownership and first-valid-answer-wins behavior.
- **REQ-SEC-003 (Accepted):** Timeout, restart, ambiguity, missing state, and
  channel failure MUST resolve to deny/no action, never approval.
- **REQ-SEC-004 (Implemented):** Tokens MUST live in private local files, not
  command arguments, JSON examples, logs, Git, documents, or Telegram content.
- **REQ-SEC-005 (Implemented):** State and secret files MUST use restrictive
  permissions; diagnostics MUST report unsafe permissions without printing
  secret values.
- **REQ-SEC-006 (Implemented):** Telegram owner, private-group, project-root,
  topic, and agent allowlists MUST be enforced before provider invocation.
- **REQ-SEC-007 (Accepted):** A provider failure MUST be visible, reversible,
  and isolated. Recovery MUST NOT weaken security policy to regain availability.

The detailed threat model in `docs/SECURITY.ru.md` remains normative where it is
more specific and consistent with this baseline.

## 13. Persistence, restart, monitoring, and recovery

- **REQ-OPS-001 (Implemented):** Routing state MUST persist locally in SQLite,
  including numeric identities, provider session IDs, active agent, writer
  state, visible-context cursors, and idempotency receipts.
- **REQ-OPS-002 (Implemented):** Telegram update processing MUST be idempotent;
  duplicate updates MUST NOT create duplicate provider turns.
- **REQ-OPS-003 (Implemented):** Schema migrations MUST be versioned and create
  an SQLite-consistent pre-migration backup with rollback on failure.
- **REQ-OPS-004 (Implemented):** User services and monitoring MUST preserve
  numeric topic/session state across ordinary restarts and report component
  health without invoking a model merely to check health.
- **REQ-OPS-005 (Implemented):** Hermes Gateway and tlive MUST be monitored as
  independent recovery channels. Failure of one is degraded service; neither is
  a mandatory dependency of Project Hub.
- **REQ-OPS-006 (Implemented):** General operational alerts are bounded,
  deduplicated, and delivered
  only to one explicitly configured Hub Operations/Alerts topic. Codex is the
  primary sender; Hermes may fall back only to that same topic. Quota alerts
  include a recognizable masked account hint and never expose a full identity.
- **REQ-OPS-007 (Implemented):** Codex rotation reacts to the upstream provider
  `429` handled by the optional multi-auth proxy, never to a forecast threshold.
  The transition is always reported to Hub Operations with masked source/target
  identity. It is also reported to the work topic only when exactly one Codex
  topic is active; multiple work topics are never spammed.
- **REQ-OPS-008 (Accepted):** On replacement hardware, stale writer leases from
  the lost host MUST be reset safely after verifying the old processes cannot
  exist.
- **REQ-OPS-009 (Implemented):** Controller, sender,
  and provider workers MUST publish bounded last-known runtime health to local
  SQLite. Cached health MUST distinguish `healthy`, `degraded`, `stale`, and
  `unknown` without invoking a model, provider API, or runtime probe. It MUST
  contain no prompts, responses, exception detail, command lines, environment
  data, credentials, or account identifiers.

**Recovery limit:** exact in-flight turns are not portable across process or
machine loss. Only completed state that was persisted before the loss can be
recovered. Git and Telegram history can help reconstruct work, but cannot
recreate unsaved provider context or a partially executed turn.

### Implemented queue compatibility and local provider-worker isolation

- **REQ-HUBBOT-001 (Implemented behind optional `hub_bot` configuration):** A separate Hub Telegram bot MUST become the
  central project-group identity. It MUST own group ingress, the universal
  command menu, and callbacks; it MUST have Privacy Mode disabled as a
  deployment prerequisite. Provider bots MUST NOT poll project groups, while
  retaining their own response identities and provider-specific direct-message
  endpoints. The Hub bot token MUST reside only in a restrictive private local
  file and MUST NOT appear in Git, examples, logs, or Telegram content. Hub
  ingress MUST use a durable offset identity distinct from Codex. When
  `hub_bot` is omitted, Codex remains the compatibility ingress without
  changing its existing offset. Controller startup MUST validate and read only
  the selected ingress token; provider workers, direct-message services, and
  outbox delivery retain their own credential and response-identity boundaries.
  Hub mode MUST use external queue/outbox ownership and MUST isolate every
  locally managed productive provider in its own external worker. A local
  runtime without external-worker support MUST be rejected rather than run
  inside the Controller.
- **REQ-QUEUE-001 (Implemented behind `dispatch_mode: "queue"`):** The deterministic Hub Controller MUST durably
  enqueue each admitted productive request before provider execution and MUST
  NOT wait for a provider CLI, RPC, or model turn to process local commands.
  A provider declared `managed_externally` MUST retain its native admission
  boundary and MUST NOT be accepted into the local durable queue. The Controller
  MUST ignore productive routes whose targets are all externally managed, MUST
  partition mixed multi-target routes before applying local admission rules,
  and MUST NOT invoke an externally managed provider merely to refresh a model
  catalog. Codex is the Controller's primary local provider and cannot be
  declared `managed_externally` in this architecture.
- **REQ-QUEUE-002 (Implemented for Codex, OpenCode, and Antigravity behind
  `queue_runtime: "external"`):** Provider execution MAY occur in one isolated
  worker per explicitly configured local agent ID. A worker owns its adapter or
  Codex app-server lifecycle and SQLite connection, leases only its own jobs,
  and has no Telegram transport or token-reading capability. The default
  external-worker list remains Codex for rollback compatibility; OpenCode and
  Antigravity are enabled independently through `external_worker_agent_ids`.
  Hermes remains externally managed and is not a queue-worker runtime. Failure,
  quota exhaustion, or restart of one worker MUST NOT block Controller commands
  or another provider's eligible work. This stage covers the shared project-group
  queue only; provider direct-message services remain separate legacy inline
  endpoints with their own state database.
- **REQ-QUEUE-003 (Implemented for the embedded compatibility consumer):** Productive jobs MUST execute strict FIFO within
  one numeric topic; different topics MAY execute concurrently. The target
  provider/session/model/effort snapshot MUST be immutable after enqueue.
- **REQ-QUEUE-004 (Implemented for the embedded compatibility consumer):** A durable job state machine MUST distinguish work
  not yet invoked from `executing`, result delivery, terminal failure, and
  `indeterminate` execution. An unproven in-flight turn MUST NOT be retried
  automatically.
- **REQ-QUEUE-005 (Implemented for embedded compatibility and the external sender):** Provider result persistence and Telegram delivery
  MUST use a durable outbox. Telegram delivery retry MUST NOT create another
  provider turn, and visible-context acknowledgement MUST occur only after a
  successful provider result commit. With external queue mode and the explicit
  `outbox_runtime: "external"` rollout gate, a standalone
  sender MUST fairly poll every locally managed queue agent, including embedded
  execution during mixed rollout, use each provider bot identity, recover only
  their stale delivery leases, and MUST NOT own a provider adapter
  or RPC client. The Controller MUST NOT deliver external-worker outbox rows.
- **REQ-QUEUE-006 (Implemented for the additive schema and global compatibility gate; per-provider rollout Planned):** Queue migration and per-provider rollout MUST be
  additive, feature-gated, recoverable through the existing backup discipline,
  and retain safe rollback without destroying accepted jobs. Changing an agent
  to `managed_externally` requires its accepted local jobs to be drained or
  explicitly reconciled first; Controller startup MUST fail visibly while any
  such nonterminal rows remain.
- **REQ-QUEUE-007 (Implemented):** Long-running Controller, direct-provider,
  worker, and outbox-sender processes MUST translate `SIGTERM` and `SIGINT`
  into cooperative stop requests only. They MUST stop polling and taking work
  at the next explicit safe boundary, return a lease when stop is observed
  before invocation without consuming an attempt, bound transport waits and
  joins, and restore prior process signal handlers. A signal may race after the
  final safe-boundary check; work past that boundary is treated as potentially
  started and remains subject to the existing `indeterminate`/outbox ambiguity
  rules rather than being made automatically retryable.

The detailed state machine, retry proof rule, reconciliation, and required
fault acceptance are normative in [ADR 0001](../decisions/0001-durable-provider-job-queue.md).

## 14. Project onboarding and Telegram acceptance

- **REQ-ONBOARD-001 (Implemented):** Project creation and root selection occur
  locally. A project is identified by stable ID, display name, canonical Git
  root, and allowlisted root boundary.
- **REQ-ONBOARD-002 (Implemented):** A Telegram group may be discovered only as
  bounded numeric/title metadata; binding requires exact local confirmation.
- **REQ-ONBOARD-003 (Accepted):** The group MUST be a private forum supergroup,
  contain the intended bot identities, and provide topic IDs. Bot permissions
  MUST be the minimum needed; lack of Manage Topics does not block General.
- **REQ-ONBOARD-004 (Accepted):** Privacy Mode and bot re-add requirements are
  deployment steps, not runtime routing actions.
- **REQ-ONBOARD-005 (Planned acceptance):** Every new project must pass a canary
  for root isolation, ordinary routing, satellite invocation, Reply routing,
  restart persistence, and correct response identity before routine use.

## 15. Functional acceptance criteria

The following are release-level acceptance criteria. Automated coverage is
necessary but not sufficient for items marked live.

- **AC-F-001 (REQ-ID-001..006):** A message in project/topic A cannot resolve,
  start, resume, or write a session in project/topic B.
- **AC-F-002 (REQ-ROUTE-001..008):** In an acceptance topic, an ordinary
  owner message invokes only the active agent; a mention invokes only the named
  satellite; a real Reply returns to the response author; a selected/pasted
  quote remains with the active agent.
- **AC-F-003 (REQ-CTX-001..007):** After a satellite exchange, the
  main agent receives the unseen visible delta on its next productive turn,
  understands its addressee, and does not answer the old message as a new task.
- **AC-F-004 (REQ-ROUTE-006):** Idle provider models show no
  provider invocation or token use during another agent's turn.
- **AC-F-005 (REQ-OPS-001..004):** A controlled restart retains
  active agent, numeric topic identity, provider session ID, ingress offset, and
  exactly-once processing.
- **AC-F-006 (REQ-WRITER-001..003):** Terminal takeover and release retain the
  Codex thread and never permit two simultaneous writers.
- **AC-F-007 (REQ-SEC-001..007):** Invalid owner/root/topic/policy, missing state,
  approval timeout, and provider failure all fail closed without secret output.
- **AC-F-008 (REQ-AUTH-002..004):** Failure of optional multi-auth degrades or
  falls back only the Codex runtime and does not stop Hub, Hermes, or tlive.
- **AC-F-009 (REQ-ONBOARD-001..004):** Telegram cannot create or rebind an
  arbitrary local project, even through crafted text, titles, quotes, or
  callback data.
- **AC-F-010 (Automated; live cutover acceptance still required;
  REQ-QUEUE-001..006):** A committed request survives Controller restart; an
  interrupted unknown provider turn is not repeated; Telegram delivery retry
  does not repeat provider work; and provider failure does not make controller
  commands or another eligible provider unavailable. The fictional
  subprocess fault matrix terminates fictional Controller, worker, and sender
  actors at the durable boundaries and covers these invariants without provider
  or Telegram network access.

## 16. Non-functional acceptance criteria

- **AC-NF-001 — Security:** Secrets, hidden reasoning, raw environment/terminal
  output, and private invite links do not appear in Git, logs, status responses,
  shared context, handoffs, or Telegram publications.
- **AC-NF-002 — Reliability:** Duplicate updates are idempotent; crashes do not
  transform pending work or approval into success; migrations are recoverable.
- **AC-NF-003 — Isolation:** One adapter or recovery-channel failure does not
  cascade into unrelated providers or projects.
- **AC-NF-004 — Observability:** Status distinguishes healthy, degraded,
  exhausted, misconfigured, and blocked components and names the bounded
  recovery action without exposing secrets.
- **AC-NF-005 — Cost control:** Passive observation and routing use deterministic
  local code; only explicitly targeted productive turns spend provider tokens.
- **AC-NF-006 — Maintainability:** Provider integration uses small versioned
  adapters and capability probes; unsupported protocol changes stop visibly
  rather than falling back to screen scraping.
- **AC-NF-007 — Upgrade compatibility:** External component versions are upgraded
  through backup, contract tests, smoke tests, health gates, and rollback. Local
  carried patches are documented before an upstream upgrade.
- **AC-NF-008 — Portability:** Publishable configuration and tests do not assume
  the owner's home path; deployment-specific paths remain local state.

## 17. Capability status matrix

| Capability | Status | Notes |
| --- | --- | --- |
| Numeric project/topic isolation | Implemented | Automated multi-project isolation tests. |
| Central Telegram group ingress | Implemented | External provider group pollers disabled by design. |
| Reply/mention/quote/ordinary semantics | Implemented | Automated routing coverage; live acceptance is deployment-local. |
| Bounded shared visible context | Implemented | Codex, OpenCode, Antigravity, and Hermes paths covered. |
| Codex persistent sessions and metadata | Implemented | App-server integration and restart persistence covered. |
| Hermes project integration | Implemented | Native Gateway plus fail-closed plugin/hook boundary. |
| OpenCode and Antigravity adapters | Implemented | Contract tests; live provider acceptance is deployment-local. |
| Codex tmux takeover/release | Implemented | Fallback frontend, not preferred long-term UX. |
| Optional Codex account pool/fallback | Implemented | Natural exhaustion E2E remains an acceptance item. |
| Telegram E2E baseline | Operator acceptance | Results remain private deployment evidence. |
| `/local` and `/return` | Implemented | Codex, OpenCode, and Antigravity; Hermes fails closed pending a native resume contract. |
| Compact command surface | Implemented | `/status`, cached/paginated `/model`, `/accounts`, confirmed `/new`, `/local`, `/return`; Telegram menu readback passed. |
| Return-and-publish | Implemented | `/return` publishes a bounded summary; no full transcript mirroring. |
| Provider-limit rotation events | Implemented | Provider `429` drives Codex rotation visibility; natural exhaustion E2E remains pending. |
| Durable embedded queue compatibility path | Implemented | `dispatch_mode: "inline"` remains default; `"queue"` with `queue_runtime: "embedded"` consumes work on a background thread. |
| Isolated local provider workers | Implemented behind feature gate | `dispatch_mode: "queue"`, `queue_runtime: "external"`, explicit `external_worker_agent_ids`, and opt-in `outbox_runtime: "external"`; controller delivery remains the default rollback path. |
| Automatic Antigravity account rotation | Deferred | Await stable supported headless account-pool capability. |
| Universal provider-neutral Session Bridge | Deferred | Add only if real adapters/companions cannot meet needs. |
| Automatic OS terminal window/PID management | Rejected | Explicit resume commands and writer leases are simpler and safer. |
| Message-by-message CLI transcript mirroring | Rejected | Provider session plus bounded publish/handoff is sufficient. |
| Automatic approval or security relaxation | Rejected | Violates the trust model. |
| New provider expansion now | Rejected | Current providers must pass E2E first. |

## 18. Known limitations

- Live acceptance is point-in-time evidence and must be repeated after material
  routing, provider, Telegram-policy, or persistence changes.
- Exact provider capability and resume flags can change between CLI versions.
- OpenCode and Antigravity lack a tlive-equivalent semantic remote companion;
  tmux/PTY remains the low-level fallback.
- Hermes native local transfer capability needs separate confirmation before a
  common `/local` experience can claim provider parity.
- Antigravity automatic rotation is unavailable without a supported headless
  account-pool interface.
- The current recovery plane handles component/service failure on the existing
  machine, not complete machine loss; machine-loss tooling is not in scope.
- Topic creation depends on the deployment bot's Telegram Manage Topics
  permission.

## 19. Maintenance and change policy

1. Update this document before or with a change to observable product behavior,
   trust boundaries, status classification, or acceptance criteria.
2. Update `docs/status/PROJECT_STATUS.md` when evidence changes a capability's
   current state.
3. Record consequential durable choices under `docs/decisions/`; do not rewrite
   accepted rationale invisibly.
4. Preserve backward compatibility for persisted state through explicit schema
   migrations and backups.
5. Prefer official provider interfaces and capability probes. Pin or test fast-
   moving optional dependencies; retain a simpler official fallback where
   feasible.
6. Prefer deterministic small adapters, bounded state, and reversible failure
   over deep CLI coupling, TUI scraping, autonomous repair, or speculative
   abstraction.
7. Store live deployment evidence outside Git and publish only reusable
   acceptance requirements or anonymized aggregate results.

## 20. Provenance

This baseline is derived from current repository behavior and automated tests.
Raw conversations, rollout logs, local configuration, and deployment identities
remain private operator state and must not be copied into Git.
