# Agents Projects Hub — product requirements baseline

Status: accepted baseline  
Version: 1.0  
Date: 2026-08-30
Product owner: repository owner

This baseline defines required outcomes and their lifecycle. Repository implementation
requires code and passing tests; live Telegram acceptance is separate.
MUST, SHOULD and MAY have their BCP 14 meanings.

## Normative modules

All modules form one baseline. Follow [AGENTS.md](../../AGENTS.md) for reading scope;
requirements, architecture, security and release/deployment changes require all modules.

- [Identity and interaction](IDENTITY_AND_INTERACTION.md)
- [Accounts, control, and security](ACCOUNTS_CONTROL_AND_SECURITY.md)
- [Persistence and recovery](PERSISTENCE_AND_RECOVERY.md)
- [Durable queue and control](DURABLE_QUEUE_AND_CONTROL.md)
- [Telegram delivery and receipts](DELIVERY_AND_RECEIPTS.md)
- [Onboarding and acceptance](ONBOARDING_AND_ACCEPTANCE.md)
- [Maintenance](MAINTENANCE.md)
- [Evaluation and resource-aware task allocation](EVALUATION_AND_ALLOCATION.md) — accepted
  foundation; implementation and acceptance pending.

[Requirement details](REQUIREMENT_DETAILS.md) preserves supporting detail by ID.
It is non-normative and does not add requirements.

## 1. Mission

Help one owner work on multiple local Git projects through private Telegram groups
and native provider interfaces. The owner can see the active project, topic, agent,
session, account mode and writer, recover saved work, and understand failures
without losing local control or incurring surprise model spend.

## 2. Product principles

1. Each project group holds one project's durable conversation and history.
2. Local deterministic code chooses recipients; provider identities stay directly addressable.
3. One coherent input normally creates one turn; passive observation MUST NOT spend model tokens.
4. A provider session MUST have one writer, across Telegram and local frontends.
5. Telegram MUST NOT select paths, relax sandboxes or grant approvals.
6. Sharing MUST contain only bounded visible dialogue, never hidden reasoning, raw tools/screens,
    credentials or environment dumps.
7. Hub, Hermes Gateway and tlive MUST remain independent recovery paths.
8. Small adapters SHOULD fail visibly and reversibly, without concealed repair or fragile CLI
    coupling.

## 3. Scope and non-goals

### In scope

Private forum groups bind to allowlisted Git roots. Topics have persistent sessions,
an active agent and explicitly addressed satellites, with bounded visible context.
Codex, Claude Code, Hermes, OpenCode and Antigravity retain their own identities,
credentials and provider boundaries.

The product supports explicit session/model controls, official Codex login and stdio
fallback, native CLI continuity, writer leases, tmux fallback, local persistence,
diagnostics, controlled upgrades and recovery planning.

### Non-goals

No hidden super-agent, remote arbitrary paths/credentials/commands/policy, automatic
approval, secret or hidden-reasoning publication, TUI scraping, OS terminal/PID
management, automatic GIFs, or per-model/account bots. No guaranteed recovery of
in-flight turns, full CLI transcript mirroring, Codex multi-account integration,
unsupported tlive semantic integration, or Hub-managed lead/advisor orchestration.

## 4. User mental model and terminology

| Term | Meaning |
| --- | --- |
| Project / project group | Immutable local project ID and allowlisted Git root / its private forum group. |
| Topic | Work stream keyed by numeric chat and thread IDs; its title is display only. |
| Agent / provider session | Addressable runtime / its own conversation in a topic. |
| Active / satellite agent | Default topic recipient / explicitly addressed agent without changing that default. |
| Visible topic journal | Bounded completed user/agent dialogue available for explicit sharing. |
| Writer lease | Persisted Telegram, local or tmux writing authority for one session. |
| Project Hub | Deterministic router/control plane. |
| Hermes Gateway | Independent Hermes Telegram/session runtime. |
| Agent Session Remote / tlive | Private control chat for supported native sessions. |

The product is **Agents Projects Hub**, displayed as **Hub** in Telegram.
The package name `hermes_codex_router` remains for compatibility.

## 5. Lifecycle labels

- **Implemented** — repository code and automated coverage; listed live acceptance may remain.
- **Accepted** — required behavior, possibly incomplete.
- **Planned** — accepted roadmap work.
- **Deferred** — waits for an explicit trigger or stable upstream capability.
- **Rejected** — outside scope unless the owner revises the baseline.
- **Retired** — withdrawn by decision; the ID stays reserved and is not completion evidence.
