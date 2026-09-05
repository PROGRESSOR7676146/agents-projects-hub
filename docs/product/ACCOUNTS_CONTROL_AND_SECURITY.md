# Accounts, control, and security requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

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
  Project Hub. Socket presence alone is insufficient health evidence: when the
  app-server's advertised multi-auth runtime proxy is unreachable, a configured
  official stdio transport MUST be selected before starting a turn. Because a
  shared app-server may retain the old thread's writer lease, fallback starts a
  new official thread and prepends only bounded persisted visible context.
- **REQ-AUTH-004A (Implemented):** A deployment MAY select
  `codex_transport: "stdio"` to always use a private official Codex app-server,
  even while the configured shared socket is healthy. This mode is recommended
  when a tlive companion watches the shared socket, because Hub project threads
  then cannot appear in Agent Session Remote.
- **REQ-AUTH-005 (Implemented):** Account changes and quota state MUST remain
  visible to the owner; switching MUST NOT be silent.
- **REQ-AUTH-006 (Accepted):** Hermes MAY guide a mode-aware manual device-login
  recovery, but MUST NOT copy or display tokens, change the wrong credential
  store, or replace deterministic locking and health checks.
- **REQ-AUTH-007 (Planned acceptance):** A natural or controlled quota-exhaustion
  test must demonstrate one response, the same persisted thread, bounded retry,
  and a visible account transition.
- **REQ-AUTH-008 (Implemented):** When tlive and the optional rotating app-server
  share a Unix control socket, boot ordering MUST wait for a successful socket
  connection rather than the presence of a socket inode. The ordering MUST NOT
  make either recovery channel a hard requirement of the other.

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
  only after a real provider `429`; plan caps are labelled separately. In the
  isolated-worker topology the Controller MUST read a bounded, masked Codex
  account snapshot from durable local state and MUST NOT invoke a provider,
  model, or account helper. Other providers MAY declare short masked account
  prefixes in private configuration; unknown limits remain explicitly unknown,
  while a provider-reported exhaustion is shown for the current unknown account.
  A configured private Antigravity status cache MAY supply structured current
  account, per-model quota, reset time, current model/effort, and matching-session
  context without ANSI parsing or a provider/model invocation. Stale, mismatched,
  oversized, or non-private cache files MUST degrade to unknown.
- **REQ-CMD-004 (Implemented):** `/new` requires an owner callback confirmation
  and resets only the active provider session; mass reset behavior is removed.
  `/local` transfers writer ownership. Codex `/return` changes only the lease,
  with no provider call, summary, or session-ID change. Other providers retain
  bounded summaries pending separate native-resume acceptance.
- **REQ-CMD-005 (Implemented):** The public Telegram command menu contains only
  `/status`, `/model`, `/accounts`, `/new`, `/local`, `/return`, and `/stop`. Legacy
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
- **REQ-CMD-007 (Implemented for queued project-group providers):** `/stop` and
  an exact case-insensitive emergency utterance (`stop`, `halt`, `стоп`, `стой`,
  `остановись`, or `прекрати`) MUST bypass model analysis. Hub cancels the
  selected active provider's not-yet-started FIFO tail and interrupts its active
  turn through provider-native control or its owned process. Matching applies
  to the complete normalized message only, never to a word embedded in prose.
  An owned external CLI process group MUST be force-stoppable even when the
  provider ignores graceful termination.
  Legacy inline OpenCode/Antigravity direct-message endpoints do not advertise
  this command until they move behind an interruptible worker boundary.

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
- **REQ-WRITER-007 (Implemented for Codex with explicit owner assertion):**
  after the owner closes the CLI and Hub work is terminal, `/return` changes
  only the lease; it invokes no model and copies no summary or transcript. The
  next Telegram turn resumes the same session. V1 does not infer OS process
  state. Other providers retain prior behavior pending separate acceptance.
- **REQ-WRITER-008 (Implemented):** Messages arriving while `local` owns the
  writer do not call a provider and explain how to return safely.

Initial reviewed resume shapes are `codex resume SESSION_ID -C ROOT`,
`opencode ROOT --session SESSION_ID`, and
`cd -- ROOT && agy --conversation SESSION_ID --sandbox --mode accept-edits`. They are version-sensitive
adapter capabilities, not permanent user-input templates. Hermes requires a
separate native capability check.

## 12. Approval, sandbox, and secret requirements

- **REQ-SEC-001 (Implemented):** Codex MUST remain `workspace-write`.
  Companion-capable shared sockets use `on-request`; isolated headless stdio,
  whether explicit or selected as fallback, uses `never` so sandboxed work may proceed but escalation cannot be
  requested. Any unexpected server approval request on that fallback MUST be
  explicitly declined. `danger-full-access`, dangerous provider bypass flags,
  and automatic approval MUST be rejected.
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
