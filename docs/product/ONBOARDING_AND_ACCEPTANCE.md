# Onboarding and acceptance requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#onboarding-and-acceptance).

## 14. Project onboarding and Telegram acceptance

- **REQ-ONBOARD-001 (Implemented):** Root authority MUST remain local: onboarding MAY accept a safe
  project ID and opaque allowed-root choice, but MUST derive one canonical direct child without
  accepting path text or forwarded/callback content as authority.
- **REQ-ONBOARD-002 (Implemented):** Group binding MUST require exact numeric workflow/root
  evidence, never titles or invite links; all execution/recovery/connect/audit paths MUST share
  current registry and canonical Git-root checks. Direct chats additionally require their configured
  project/owner, and unrelated invalid projects MUST NOT hide healthy ones.
- **REQ-ONBOARD-003 (Implemented offline; live acceptance required):** Ready groups MUST be private
  forums containing Hub, local provider bots and every confirmed owner, with verified human
  administrative recovery authority before bots are invited.
  - External providers MUST retain native membership/admission without provisioner intervention; bot
    rights MUST be minimal and General usable without Manage Topics.
  - Existing exact active membership MUST skip invitation; only proven absence permits it, and
    unknown, banned, left or mismatched membership MUST block readiness.
- **REQ-ONBOARD-004 (Accepted):** Privacy Mode and bot re-addition MUST remain deployment steps,
  never agent-switch actions.
- **REQ-ONBOARD-005 (Planned acceptance):** Each new project MUST pass isolation, routing,
  satellite, Reply, restart and response-identity canaries before routine use.
- **REQ-ONBOARD-006 (Implemented offline; deployment opt-in required):** Automated provisioning MAY
  use only an explicitly enabled private user session pinned to an owner, distinct from the
  acceptance actor. Credentials/sidecars MUST stay private, one process MUST own login/provisioning,
  and mutations MUST have bounded waits without automatic retry/reconnect.
- **REQ-ONBOARD-007 (Implemented):** Onboarding MUST persist external-operation boundaries, retain
  blocked project/root reservations, and pause unknown creation/configuration outcomes without blind
  retry or deletion.
  - Proven rejection MAY resume only with exact local confirmation, fresh owners and consistent
    identities; unknown completion delivery MUST remain unknown.
  - Notices/command convergence MUST retain bounded retries and shared bot cooldowns, fail
    exhaustion safely and require explicit local reset; audit MUST remain read-only without
    initialization/migration.

### Existing-project editing

- **REQ-PROJECT-EDIT-001 (Implemented):** Owner editing MUST first select a registered project;
  local display-name changes MUST NOT rename its Telegram group without separate explicit
  confirmation.
- **REQ-PROJECT-EDIT-002 (Implemented):** Relocation MUST offer opaque local safe-root choices,
  never Telegram paths; targets MUST be canonical direct children without symlinks, escapes,
  duplicates or nonempty non-Git content, and only the safely derived project directory MAY be
  initialized.
- **REQ-PROJECT-EDIT-003 (Implemented):** Relocation MUST preserve project/group/topic identity,
  files and history without moving, copying or deleting roots. Attached sessions, other writers,
  work, delivery or uncertainty MUST block it; archived origins MUST stay at their original roots.
- **REQ-PROJECT-EDIT-004 (Implemented):** Editing MUST require durable preview/confirmation and
  commit registry, execution scope and binding together; faults MUST roll back or recover before new
  input, and repeated callbacks MUST return the same result.

## 15. Functional acceptance criteria

Release acceptance requires automated evidence; marked live checks remain separate.

- **AC-F-001 (REQ-ID-001..006):** Project/topic A cannot access or write B's sessions.
- **AC-F-002 (REQ-ROUTE-001..008):** Ordinary, mention and real Reply input reach only their
  intended agents; quotes stay context and forwards execute nothing.
- **AC-F-003 (REQ-CTX-001..007):** No automatic cross-agent history appears; explicit context
  supplies only bounded attributed topic dialogue as data.
- **AC-F-004 (REQ-ROUTE-006):** Idle providers spend no tokens or invocations during another agent's
  turn.
- **AC-F-005 (REQ-OPS-001..004):** Restart preserves identities, sessions, active agent, ingress
  offset and idempotency.
- **AC-F-006 (REQ-WRITER-001..003):** Takeover/release preserves the Codex thread and one writer.
- **AC-F-007 (REQ-SEC-001..007):** Invalid authority/policy, missing state, timeout and failure
  refuse safely without secrets.
- **AC-F-008 (REQ-AUTH-002, REQ-AUTH-004):** Shared-server failure remains Codex-local; retired
  multi-auth configuration fails by key name.
- **AC-F-009 (REQ-ONBOARD-001..007):** Onboarding cannot escape its locally derived root or steal a
  group binding through crafted Telegram input.
- **AC-F-010 (Automated; live cutover acceptance still required; REQ-QUEUE-001..006):** Offline
  fault acceptance MUST prove committed input survives, uncertainty never replays, delivery never
  repeats execution, and independent commands/providers remain usable. Unresolved same-root work
  stays blocked until immutable owner resolution.
- **AC-F-011 (Implemented scaffold; deployment authorization pending):** A dedicated acceptance user
  MAY run only the fixed nondestructive baseline in one authorized group/topic, with private
  credentials/evidence and no global-owner authority.
  - Unexpected senders invalidate checks, the first failure stops the run, and bots MUST NOT
    impersonate the owner.
  - Optional Codex P0/P1 checks MUST require explicit restart opt-in, private state, aligned
    identity and initially active fixed units, then restore those units.
  - Configuration MUST NOT supply arbitrary prompts, commands or services; the seven fixed
    material/FIFO/restart/telemetry/status results remain distinct.
- **AC-F-012 (Automated offline; REQ-OPS-010..011):** Exact clean candidate and distinct compatible
  rollback artifacts MUST pass identity/digest/schema checks and temporary migration/rollback
  without changing queued, delivery or uncertain work; this proves no live rollout.
- **AC-F-013 (Automated offline; live Telegram acceptance pending; REQ-CMD-008,
  REQ-WRITER-009..012):** All connect entries MUST select the exact saved thread without inference,
  preserve root/owner/binding safety through stale, duplicate, concurrent or unknown outcomes, and
  activate once behind the marker.
- **AC-F-014 (Automated offline; REQ-PROJECT-EDIT-001..004):** Editing MUST preserve
  identity/history, refuse unsafe or busy relocation, recover crashes once and resolve new work to
  the new root while archived origins retain the old one.
- **AC-F-015 (Retired by ADR 0065):** Role-workflow acceptance is removed, not passed; its ID stays
  reserved. Claude routing, approvals, installation, Telegram and exact-session parity retain
  separate gates; the [journal](EVALUATION_AND_ALLOCATION.md#acceptance-boundaries) remains
  independent and deferred transfer/discovery/images do not gate the bounded editor.

## 16. Non-functional acceptance criteria

- **AC-NF-001 — Security:** Secrets, reasoning, raw environment/screens and private invite links
  never enter Git, logs, status, shared context, handoffs or Telegram.
- **AC-NF-002 — Reliability:** Duplicates are idempotent; crashes never turn pending work/approval
  into success; migrations recover.
- **AC-NF-003 — Isolation:** Failures stay within their provider/channel/project.
- **AC-NF-004 — Observability:** Status distinguishes health, degradation, exhaustion,
  misconfiguration and blocking, with safe recovery actions.
- **AC-NF-005 — Cost control:** Passive routing/observation is deterministic; only explicitly
  targeted work spends tokens.
- **AC-NF-006 — Maintainability:** Small versioned adapters/probes refuse unsupported changes
  visibly, without screen scraping.
- **AC-NF-007 — Upgrade compatibility:** Upgrades require backup, contract/smoke checks, health
  gates and rollback; carried patches are documented first.
- **AC-NF-008 — Portability:** Published configuration/tests use no owner-specific paths.

## 17. Capability status matrix

| Capability | Status | Boundary |
| --- | --- | --- |
| Numeric isolation, central ingress, routing, explicit context | Implemented | Live acceptance stays deployment-local. |
| Inbound materials | Implemented for Hub-owned durable queue ingress; live acceptance pending | Verified content or visible refusal. |
| Artifact delivery | Implemented for Hub-owned transports | Private verified snapshots; Hermes remains independent. |
| Codex sessions/metadata, Hermes integration, OpenCode/Antigravity adapters | Implemented | Provider-specific live acceptance remains. |
| Codex tmux takeover/release | Implemented | Fallback frontend. |
| Codex login/stdio | Implemented | Multi-auth retired. |
| Telegram E2E baseline | Bounded actor implemented; live authorization pending | Private evidence. |
| Local/return | Implemented | Codex same-session/model-free; other providers need acceptance. |
| Onboarding | Implemented offline; deployment opt-in and live canary required | Owner-only provisioning. |
| Project editing | Implemented offline; live Telegram acceptance pending | Safe local changes only. |
| Compact controls | Implemented | Scoped supported menus. |
| Saved Codex connect | Implemented; live Telegram acceptance pending | One exact-thread workflow. |
| Summary-free Codex return | Implemented | Lease only. |
| Codex rotation events | Retired | No pool integration. |
| Embedded queue | Implemented | Inline remains default. |
| External workers/root concurrency | Implemented behind feature gate | Explicit capacity and validated worktrees. |
| Antigravity rotation; universal Session Bridge | Deferred | Requires stable capability or demonstrated need. |
| OS terminal/PID management; full CLI mirroring; automatic approval/security relaxation | Rejected | Outside scope. |
| Claude worker | Repository scaffold; live acceptance pending | Explicit routes/tools; parity deferred, no same-UID isolation claim. |
| Incident/update plane | Planned | Passive notices; owner-authorized analysis and updates. |
