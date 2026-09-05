# Agents Projects Hub — product requirements baseline

Status: accepted baseline  
Version: 1.0  
Date: 2026-08-30
Product owner: repository owner

This index and its linked modules are the primary product orientation for future
agents. They record accepted intent and distinguish current, planned, deferred,
and rejected behavior. Current implementation claims are bounded by code and
passing tests; the live Telegram E2E baseline remains a separate acceptance
step.

Normative words MUST, SHOULD, and MAY are used in their BCP 14 sense.

## Normative modules

The index and every module below form one accepted baseline. Read all modules
before changing observable product behavior or trust boundaries.

- [Identity and interaction](IDENTITY_AND_INTERACTION.md)
- [Accounts, control, and security](ACCOUNTS_CONTROL_AND_SECURITY.md)
- [Persistence and recovery](PERSISTENCE_AND_RECOVERY.md)
- [Onboarding and acceptance](ONBOARDING_AND_ACCEPTANCE.md)
- [Maintenance](MAINTENANCE.md)

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
4. **One coherent user input, one provider turn by default.** A short burst of
   consecutive compatible Telegram messages is one coherent input. Passive
   observation MUST NOT invoke paid models.
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
- Full transcript mirroring between Telegram and every native CLI. Productive
  Telegram inputs are still delivered durably to their selected provider.
- Screen scraping provider TUIs or automatic OS terminal/PID orchestration.
- Automatic GIF selection or delivery.
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
