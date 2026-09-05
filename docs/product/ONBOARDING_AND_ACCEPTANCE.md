# Onboarding and acceptance requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

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
  quote remains with the active agent; and a forwarded message is stored as
  passive context without executing forwarded commands.
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
- **AC-F-011 (Implemented scaffold; deployment authorization pending):** A
  dedicated MTProto acceptance user MAY execute the fixed non-destructive live
  baseline. Hub MUST authorize it only for one exact configured group/topic;
  the actor MUST reject arbitrary configured commands and prompts, keep all
  credentials/session/evidence outside Git, and never be treated as a global
  owner. Traffic from any sender outside the pinned actor, Hub, and configured
  provider identities MUST invalidate the affected canary check rather than be
  mistaken for test output. The runner MUST stop after the first failed check so
  it cannot enqueue unrelated later scenarios behind unhealthy provider work.
  Bot identities MUST NOT be used to impersonate the operator because
  Telegram does not deliver bot-authored messages to other bots.

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
| Explicit bounded visible context | Implemented | No automatic handoff; `/context [agent_id] [1..20]` reads only the current topic on explicit user request. |
| Artifact staging and attachment delivery | Implemented for Hub-owned transports | Exact per-job staging, private immutable spool, path/size/digest validation, bounded rejection notice, durable ordered queue delivery, immediate legacy/DM delivery, and post-acceptance cleanup. Hermes retains its independent native transport. |
| Codex persistent sessions and metadata | Implemented | App-server integration and restart persistence covered. |
| Hermes project integration | Implemented | Native Gateway plus fail-closed plugin/hook boundary. |
| OpenCode and Antigravity adapters | Implemented | Contract tests; live provider acceptance is deployment-local. |
| Codex tmux takeover/release | Implemented | Fallback frontend, not preferred long-term UX. |
| Optional Codex account pool/fallback | Implemented | Natural exhaustion E2E remains an acceptance item. |
| Telegram E2E baseline | Bounded actor implemented; live authorization pending | Results remain private deployment evidence. |
| `/local` and `/return` | Implemented | Codex return is model-free and same-session; other providers retain prior behavior pending acceptance. |
| Compact command surface | Implemented | `/status`, cached/paginated `/model`, `/accounts`, confirmed `/new`, `/local`, `/return`; Telegram menu readback passed. |
| Summary-free Codex return | Implemented | Local lease change; no model, transcript, handoff, or session change. |
| Provider-limit rotation events | Implemented | Provider `429` drives Codex rotation visibility; natural exhaustion E2E remains pending. |
| Durable embedded queue compatibility path | Implemented | `dispatch_mode: "inline"` remains default; `"queue"` with `queue_runtime: "embedded"` consumes work on a background thread. |
| Isolated local provider workers | Implemented behind feature gate | `dispatch_mode: "queue"`, `queue_runtime: "external"`, explicit `external_worker_agent_ids`, and opt-in `outbox_runtime: "external"`; controller delivery remains the default rollback path. |
| Automatic Antigravity account rotation | Deferred | Await stable supported headless account-pool capability. |
| Universal provider-neutral Session Bridge | Deferred | Add only if real adapters/companions cannot meet needs. |
| Automatic OS terminal window/PID management | Rejected | Explicit resume commands and writer leases are simpler and safer. |
| Message-by-message CLI transcript mirroring | Rejected | Provider sessions plus explicit bounded history retrieval are sufficient. |
| Automatic approval or security relaxation | Rejected | Violates the trust model. |
| New provider expansion now | Rejected | Current providers must pass E2E first. |
