# Agents Projects Hub — product requirements baseline

Status: accepted baseline  
Version: 1.0  
Date: 2026-08-29 (Europe/Moscow)  
Product owner: repository owner  
Repository: `PROGRESSOR7676146/agents-projects-hub`

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

Current registered real projects are:

| Project ID | Canonical root | Telegram display |
| --- | --- | --- |
| `hub` | `/home/unbound/src/agents-projects-hub` | Hub |
| `pythia` | `/home/unbound/src/Pythia` | Pythia |
| `babelfish` | `/home/unbound/src/Babelfish` | Babelfish |

These deployment paths are current local identities, not portable defaults for
other users. Publishable configuration MUST remain path-agnostic.

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
| Antigravity | Implemented adapter | `agy` conversation in sandboxed plan mode; no dangerous permission bypass. |
| Gemini CLI | Rejected for active product | Google provider work uses Antigravity; do not reactivate a parallel Gemini CLI path without a new decision. |

Provider bot identity maps to a runtime, not to a model or paid account. Model
selection belongs inside the agent session and should remain visible in status
or response metadata when the provider exposes it.

## 10. Codex accounts and optional multi-auth

- **REQ-AUTH-001 (Implemented):** Only paid accounts explicitly allowed by the
  owner are in scope: `7676146@gmail.com` and `prgrssr@gmail.com`. Unpaid
  discovered accounts MUST NOT be selected for project work.
- **REQ-AUTH-002 (Implemented):** `codex-multi-auth` is an optional accelerator,
  not a Project Hub dependency.
- **REQ-AUTH-003 (Implemented):** When a healthy rotating app-server is
  configured, Hub MAY use its account pool while preserving the same Codex
  thread and exposing bounded quota/account health without tokens.
- **REQ-AUTH-004 (Implemented):** When multi-auth is unavailable, Hub MUST be
  able to use the official Codex stdio app-server rather than fail the entire
  Project Hub.
- **REQ-AUTH-005 (Accepted):** Account changes and quota state MUST remain
  visible to the owner; switching MUST NOT be silent.
- **REQ-AUTH-006 (Accepted):** Hermes MAY guide a mode-aware manual device-login
  recovery, but MUST NOT copy or display tokens, change the wrong credential
  store, or replace deterministic locking and health checks.
- **REQ-AUTH-007 (Planned acceptance):** A natural or controlled quota-exhaustion
  test must demonstrate one response, the same persisted thread, bounded retry,
  and a visible account transition.

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
  CLI and confirming that no Hub dispatch is running. V1 deliberately does not
  infer OS process state.
- **REQ-WRITER-008 (Implemented):** Messages arriving while `local` owns the
  writer do not call a provider and explain how to return safely.
- **REQ-WRITER-009 (Planned):** `/publish` will publish a bounded safe summary of
  the local interval to Telegram; it will not promise full transcript import.

Initial reviewed resume shapes are `codex resume SESSION_ID -C ROOT`,
`opencode ROOT --session SESSION_ID`, and
`cd -- ROOT && agy --conversation SESSION_ID --sandbox --mode plan`. They are version-sensitive
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
- **REQ-OPS-006 (Implemented):** Alerts are bounded, deduplicated, and delivered
  only to one explicitly configured Hub Operations/Alerts topic. Codex is the
  primary sender; Hermes may fall back only to that same topic. Quota alerts
  include a recognizable masked account hint and never expose a full identity.
- **REQ-OPS-007 (Planned):** A curated encrypted recovery bundle will include a
  consistent Hub database, deployment manifest, necessary provider session
  stores, versions, and checksums, with backup/verify/restore commands and a
  real restore drill.
- **REQ-OPS-008 (Planned):** Recovery MUST exclude logs, caches, sockets, PIDs,
  tmux state, binaries, and raw environment dumps; encryption keys remain off
  the protected machine.
- **REQ-OPS-009 (Accepted):** On replacement hardware, stale writer leases from
  the lost host MUST be reset safely after verifying the old processes cannot
  exist.

**Recovery limit:** exact in-flight turns are not portable across process or
machine loss. Only completed state that was persisted before the loss can be
recovered. Git and Telegram history can help reconstruct work, but cannot
recreate unsaved provider context or a partially executed turn.

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
- **AC-F-002 (REQ-ROUTE-001..008, live pending):** In Hub General, an ordinary
  owner message invokes only the active agent; a mention invokes only the named
  satellite; a real Reply returns to the response author; a selected/pasted
  quote remains with the active agent.
- **AC-F-003 (REQ-CTX-001..007, live pending):** After a satellite exchange, the
  main agent receives the unseen visible delta on its next productive turn,
  understands its addressee, and does not answer the old message as a new task.
- **AC-F-004 (REQ-ROUTE-006, live pending):** Idle provider models show no
  provider invocation or token use during another agent's turn.
- **AC-F-005 (REQ-OPS-001..004, live pending):** A controlled restart retains
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
| Numeric project/topic isolation | Implemented | Automated tests and live Pythia/Babelfish deployment history. |
| Central Telegram group ingress | Implemented | External provider group pollers disabled by design. |
| Reply/mention/quote/ordinary semantics | Implemented | Automated coverage; Hub General live E2E pending. |
| Bounded shared visible context | Implemented | Codex, OpenCode, Antigravity, and Hermes paths covered. |
| Codex persistent sessions and metadata | Implemented | App-server integration and restart persistence covered. |
| Hermes project integration | Implemented | Native Gateway plus fail-closed plugin/hook boundary. |
| OpenCode and Antigravity adapters | Implemented | Local live provider probes passed; full Hub General E2E pending. |
| Codex tmux takeover/release | Implemented | Fallback frontend, not preferred long-term UX. |
| Optional Codex account pool/fallback | Implemented | Natural exhaustion E2E remains an acceptance item. |
| Hub General Telegram E2E baseline | Implemented/live accepted | Passed ordinary, satellite, Reply, context, no-idle-spend, identity, and restart cases on 2026-08-29. |
| `/local` and `/return` | Implemented | Codex, OpenCode, and Antigravity; Hermes fails closed pending a native resume contract. |
| Bounded `/publish` | Planned | Summary only, no full transcript mirroring. |
| Encrypted disaster-recovery bundle | Planned | Includes a restore drill and off-machine key custody. |
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
- The current disaster recovery plane handles component/service failure on the
  existing machine, not complete machine loss; the encrypted bundle is planned.
- The Hub bot can publish in Hub General but cannot create more forum topics
  without Telegram Manage Topics permission.

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
7. Do not begin roadmap step 4 (`/local`/`/return`) until this baseline is
   reviewed and the step 3 Hub General live E2E is complete.

## 20. Provenance

This baseline was derived from current repository behavior, automated tests,
the accepted 2026-08-29 product handoff, and the three sanitized visible-session
exports in `docs/history/`. Raw Codex rollouts remain private local state and
must not be copied into Git or Telegram.
