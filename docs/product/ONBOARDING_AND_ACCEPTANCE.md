# Onboarding and acceptance requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 14. Project onboarding and Telegram acceptance

- **REQ-ONBOARD-001 (Implemented):** Project root authority remains local. The
  private Hub onboarding wizard MAY accept a safe project ID and an opaque
  choice among configured `allowed_roots`; it MUST derive a single direct-child
  root locally and MUST NOT accept a path, separator, dot component, callback
  path or forwarded text as root authority. A project is identified by stable
  ID, display name, canonical Git root, and allowlisted root boundary.
- **REQ-ONBOARD-002 (Implemented):** A Telegram group may be discovered only as
  bounded numeric/title metadata. A newly created group is bound only through
  the exact durable workflow receipt for its numeric identity and canonical
  root; titles and invite links never authorize a binding. Controller ingress,
  provider execution, recovery, saved-session connection and command audit MUST
  resolve static and dynamic bindings through the same current-registry,
  allowlisted-root and real-Git-toplevel checks. Direct-message work additionally
  requires the exact configured direct project and a configured owner chat ID.
  Resolving one project MUST NOT fail because an unrelated project is disabled;
  listing/audit MUST retain healthy groups and report bounded per-group errors.
- **REQ-ONBOARD-003 (Implemented offline; live acceptance required):** The group
  MUST be a private forum supergroup,
  contain the Hub and every locally managed provider bot identity, contain every
  owner captured when the action was confirmed, and provide topic IDs. A
  provider declared `managed_externally` retains its native admission boundary;
  its group membership is optional and the provisioner MUST NOT preflight,
  invite, promote or verify it. The creating technical owner remains
  creator; every other captured owner MUST be invited, granted the documented
  administrative rights, and verified before any bot invitation begins, so a
  later bot-specific rejection leaves a human recovery authority in the group.
  Before inviting a bot, the worker MUST read its exact active group membership
  and skip the invitation when the expected identity is an active participant. It MUST invite only
  after Telegram proves `UserNotParticipant`; other lookup failures MUST NOT be
  treated as absence. Left, banned, unknown and mismatched participant results
  MUST block configuration and final readiness.
  Bot permissions MUST be the
  minimum needed; lack of Manage Topics does not block General.
- **REQ-ONBOARD-004 (Accepted):** Privacy Mode and bot re-add requirements are
  deployment steps, not runtime routing actions.
- **REQ-ONBOARD-005 (Planned acceptance):** Every new project must pass a canary
  for root isolation, ordinary routing, satellite invocation, Reply routing,
  restart persistence, and correct response identity before routine use.
- **REQ-ONBOARD-006 (Implemented offline; deployment opt-in required):** Because
  Telegram group creation and participant invitation are user-only MTProto
  operations, full automation MAY use a separate explicitly enabled local user
  session. Its API hash and session remain private files, its numeric identity
  MUST be pinned to an owner, and the long-running provisioner MUST refuse an
  unpinned or different identity. The user session and its sidecars MUST remain
  private and one nonblocking process lock MUST cover login and the whole worker
  lifetime. Mutation RPCs MUST use bounded deadlines with client retry and
  reconnect disabled. The acceptance actor is not reused.
- **REQ-ONBOARD-007 (Implemented):** Project onboarding MUST persist external
  operation boundaries. Root preparation is idempotent; an unknown group create
  or bot-configuration outcome MUST pause without blind retry or deletion.
  A preflight or proven configuration rejection MUST remain explicitly resumable
  without inventing a group identity. Recovery requires an exact local
  workflow/group confirmation, a fresh owner snapshot, and MUST reject
  conflicting project, group or root identities. Completion notices and new
  group command scopes MUST retain durable retry deadlines. A recoverable block
  MUST continue reserving its project ID and root. Command convergence MUST use
  one bot/group API operation per leased step, persist per-bot cooldowns that
  apply to bindings created during the cooldown, fail exhausted work safely,
  and permit only explicit local reset. Command audit MUST open existing state
  read-only without initialization or migration. An ambiguous completion send
  without a positive Telegram message ID MUST remain unknown.

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
- **AC-F-003 (REQ-CTX-001..007):** After a satellite exchange, the main
  agent receives no automatic unseen-dialogue injection. An explicit `/context`
  request supplies only the selected bounded visible topic history, with its
  attribution, and does not reinterpret old messages as new tasks.
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
- **AC-F-009 (REQ-ONBOARD-001..007):** Telegram cannot select or rebind an
  arbitrary local path. Project creation is bounded to one safe project ID and
  one opaque `allowed_root` option; crafted text, titles, quotes, forwards and
  callback data cannot escape that derived root or steal a numeric group
  binding.
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
- **AC-F-012 (Automated offline; REQ-OPS-010..011):** Distinct clean candidate
  and rollback wheels pass the digest/identity/schema manifest gate; a
  temporary schema-20 production-shaped database migrates to the candidate target; the
  temporary activation pointer switches to the candidate and back; and both
  compatible artifacts open the retained target state without changing queued,
  outbox, or indeterminate work. This is not a live rollout or Telegram E2E.
- **AC-F-013 (Automated offline; live Telegram acceptance pending;
  REQ-CMD-008, REQ-WRITER-009..012):** Topic, Hub-private, and local-code entry
  paths select one exact saved Codex thread without model inference. Empty,
  occupied and newly created destinations preserve project/root/owner checks.
  Expired, repeated and concurrent codes, stale callbacks, changed targets,
  delayed input, restart boundaries, metadata failure, and unknown Telegram
  send/create outcomes neither substitute a thread nor partially archive the
  existing binding. A committed marker activates one generation; the next
  later message resumes the selected thread without `/return`.

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
| Project/group onboarding | Implemented offline; deployment opt-in and live canary required | Owner-only Hub wizard, schema-28 owner/lease/command receipts, bounded Git-root preparation and a separately authorized owner user-session provisioner; unknown Telegram outcomes stop without blind retry. |
| Compact command surface | Implemented | Provider defaults remain bounded; project Hub scope exposes `/menu`, `/connect`, `/stop`, and Hub private scope exposes `/start`, `/projects`, `/connect`, `/cancel`. |
| Saved Codex session connect | Implemented; live Telegram acceptance pending | One durable workflow serves topic selection, Hub-private selection/new-topic creation, and owner-scoped local one-time codes. |
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
