# Accounts, control, and security requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 10. Codex accounts and transports

- **REQ-AUTH-001 (Implemented):** Only account profiles explicitly allowlisted
  by the operator are in scope. Discovered but unapproved accounts MUST NOT be
  selected for project work, and account identifiers MUST remain outside Git.
- **REQ-AUTH-002 (Implemented; ADR 0047):** Codex runs only under the
  operator's official Codex login, through the configured shared app-server
  socket or the official stdio app-server. Project Hub has no Codex
  multi-account integration. The retired configuration keys
  `codex_multi_auth_dir`, `codex_multi_auth_executable`, and
  `codex_account_hints` MUST fail configuration loading, naming the keys, before
  any filesystem or helper access.
- **REQ-AUTH-003 (Retired by ADR 0047):** Hub MUST NOT read, execute, display, or
  alert on a Codex account pool. State written by an earlier release, such as a
  pool snapshot or a latched pool alert, MUST be ignored or released rather than
  presented as current.
- **REQ-AUTH-004 (Implemented):** When the shared Codex app-server is
  unavailable, Hub MUST be able to use the official Codex stdio app-server
  rather than fail the entire Project Hub. Socket presence alone is insufficient
  health evidence: a socket that refuses connection MUST select a configured
  official stdio transport before starting a turn. Because a shared app-server
  may retain the old thread's writer lease, fallback starts a new official
  thread and prepends only bounded persisted visible context. Explicitly adopted
  CLI threads are an exception: stdio MUST resume their exact identity or fail
  visibly, never substitute a new thread or a summary.
- **REQ-AUTH-005 (Implemented):** Account changes and quota state MUST remain
  visible to the owner; switching MUST NOT be silent.
- **REQ-AUTH-006 (Accepted):** Hermes MAY guide a mode-aware manual device-login
  recovery, but MUST NOT copy or display tokens, change the wrong credential
  store, or replace deterministic locking and health checks.
- **REQ-AUTH-007 (Retired by ADR 0047):** Hub no longer switches Codex
  accounts, so no account-transition acceptance applies.
- **REQ-AUTH-008 (Retired by ADR 0047):** The repository no longer ships unit
  ordering for a rotating app-server. tlive and any shared app-server remain
  independent; neither is a hard requirement of the other.
- **REQ-AUTH-009 (Claude repository scaffold; live acceptance pending):** A
  Hub-owned Claude Code CLI turn routed through CPA MUST require an explicit
  loopback `ANTHROPIC_BASE_URL` and exactly one CPA client credential source
  before invocation. Cloud-provider selectors and an ambiguous mix of bearer
  token and API key MUST fail before a provider process starts. The private CPA
  configuration MUST exclude paid/API and extra-usage fallback before deployment;
  a local URL check alone does not prove its upstream account or billing route.
  The default worker MUST expose no Claude built-in tools, customizations,
  skills, or MCP servers and MUST deny permission prompts. An explicit local
  file-tool opt-in MAY use the protected human boundary of REQ-SEC-008;
  deployment and native/Telegram acceptance remain separate gates. It MUST NOT
  claim complete provider parity or authority-data custody on the strength of
  prompt instructions or this limited tool slice alone.

### Compact control surface

- **REQ-CMD-001 (Implemented):** `/status` shows the active provider, model,
  effort, context remainder when observable, masked active account when the
  provider reports one, and compact
  provider-supplied limit/reset windows. Codex context remainder MUST use the
  latest current-context usage snapshot, not cumulative lifetime token usage;
  a missing current snapshot is unknown and MUST clear an older displayed
  value. A quota-window label MUST derive from the provider-reported duration.
  `primary` and `secondary` identify positions only: an unknown duration remains
  `Primary window` or `Secondary window` rather than being guessed as five-hour
  or weekly. A Codex window missing from the account snapshot MAY come from the
  rolling rate-limit update the provider sent during the same turn; it serves
  only that turn's response and MUST NOT be shown later as current.
  A final Codex response MAY include an observed `/goal` status only from
  payload-free native events bound to the exact accepted thread and turn.
  Active, paused, blocked, usage-limited, budget-limited and complete MUST remain
  distinguishable. The label MUST say `observed`: it describes the latest exact
  event consumed during submission/wait, not the state at native completion or
  when Telegram receives it. Foreign or unbound events, clear, malformed state,
  exhausted optional bounds and a new submission MUST NOT become stale or
  inferred mode claims. Unknown modes MUST be omitted rather than shown as off.
  Observation MUST add no RPC, inference, mode change or execution authority and
  MUST NOT interrupt mandatory result, approval or telemetry consumption.
  Delivery retries MUST retain the saved footer; completion-only recovery MUST
  NOT borrow today's thread state. Prompt text and preparatory/subsequent-turn
  service-tier settings MUST NOT establish current-turn `/fast` evidence.
- **REQ-CMD-002 (Implemented):** `/model` is the single cascaded selector for
  provider, model, and effort. It marks current values and validates callbacks
  against the exact cached catalog snapshot displayed to the user. The final
  click changes local session state deterministically and MUST NOT depend on a
  new provider RPC or an AI-generated handoff.
  Claude MAY expose an explicit local `model_catalog`: 1–32 unique ASCII model
  IDs of at most 128 characters, printable labels of 1–96 characters, and
  unique nonempty effort subsets of `low`, `medium`, `high`, `xhigh`, and `max`.
  Every entry contains only `model_id`, `label`, and `efforts`; the configured
  default model/effort pair MUST be included. Omission retains the single
  configured default. This field MUST be rejected for other runtimes. Claude
  menus MUST label these as configured choices with availability unverified;
  neither entitlement, effective effort nor billing route is established.
- **REQ-CMD-002A (Implemented):** Successful provider discovery updates a
  private atomic last-known-good catalog with source version and timestamp.
  Telegram callbacks use bounded opaque keys rather than provider model IDs;
  long catalogs are paginated. Failed discovery uses the cache and becomes an
  Operations warning only after the cached success is older than 24 hours.
  The monitor reads native Codex `model/list` metadata through the configured
  socket, without starting threads or inference, and MUST NOT execute an account
  helper; a Codex catalog cached from any other source is replaced on the next
  refresh. Discovery failures preserve the last good catalog. The isolated Controller
  never discovers models itself: Refresh requests monitor refresh while keeping
  cached choices usable. Only an empty cache uses the configured default.
  Claude catalog display, callback validation and monitor refresh MUST project
  the same current local configuration without subprocess, network, provider
  or account-helper invocation. A configuration change MUST invalidate removed
  model/effort choices even when the cache is fresh. An already selected choice
  outside the updated catalog MUST remain visibly selected without an automatic
  reset, session replacement or false availability claim.
- **REQ-CMD-003 (Implemented):** `/accounts` lists configured provider accounts
  and observable limits. OpenCode Go exact exhaustion/reset telemetry is shown
  only after a real provider `429`; plan caps are labelled separately. The
  Controller MUST build `/accounts` from private configuration and durable local
  state only and MUST NOT invoke a provider, model, or account helper. Codex has
  no account list (REQ-AUTH-003): a Codex agent in `provider_account_hints` MUST
  fail configuration loading. Other providers MAY declare short masked account
  prefixes in private configuration; unknown limits remain explicitly unknown,
  while a provider-reported exhaustion is shown for the current unknown account.
  A configured private Antigravity status cache MAY supply structured current
  account, per-model quota, reset time, current model/effort, and matching-session
  context without ANSI parsing or a provider/model invocation. Stale, mismatched,
  oversized, or non-private cache files MUST degrade to unknown.
- **REQ-CMD-004 (Implemented):** `/new` requires an owner callback confirmation
  and resets only the active provider session; mass reset behavior is removed.
  `/local` transfers writer ownership. Codex `/return` changes only the lease,
  with no provider call, summary, or session-ID change. Codex local commands
  attach the native TUI to the configured owning app-server through `--remote`
  and a Unix socket; standalone resume MUST NOT create a competing persistence
  writer. Remote attach retains the session's permissions without overrides.
  Other providers retain
  bounded summaries pending separate native-resume acceptance.
- **REQ-CMD-005 (Implemented):** The public Telegram command menu contains only
  `/status`, `/model`, `/accounts`, `/new`, `/local`, `/return`, and `/stop`. Legacy
  maintenance commands may remain locally callable for compatibility but are
  not part of the normal mobile interface. In registered project groups only
  the central router bot publishes `/menu`, `/connect`, and `/stop`; provider bots publish
  empty chat-scoped menus so Telegram does not duplicate commands with bot
  username suffixes. Direct provider chats expose only commands implemented by
  that provider endpoint. The Hub private chat publishes `/start`, `/projects`,
  `/connect`, and `/cancel`. A deterministic local command checks and
  synchronizes every scope.
- **REQ-CMD-006 (Implemented):** Provider, model, and effort buttons mark the
  active choice and use Telegram's success style where supported. Account and
  quota summaries use portable green/yellow/red status symbols because message
  text itself has no reliable cross-client color API.
- **REQ-CMD-007 (Implemented for queued project-group providers):** `/stop` and
  an exact case-insensitive emergency utterance (`stop`, `halt`, `стоп`, `стой`,
  `остановись`, or `прекрати`) MUST bypass model analysis. Hub cancels every
  not-yet-started FIFO job in the numeric topic, whatever its provider, and
  interrupts the topic's running turn, which topic FIFO limits to one, through
  that provider's native control or its owned process, even when the provider
  was invoked by mention rather than being the active agent. Held work awaiting
  an owner decision is left for that decision. Matching applies
  to the complete normalized message only, never to a word embedded in prose.
  An owned external CLI process group MUST be force-stoppable even when the
  provider ignores graceful termination.
  Legacy inline OpenCode/Antigravity direct-message endpoints do not advertise
  this command until they move behind an interruptible worker boundary.
- **REQ-CMD-008 (Implemented; live Telegram acceptance pending):** `/connect` in
  a registered project topic, `/connect` in the owner-only Hub private control
  plane, and local `session connect [CONFIG]` MUST use one durable deterministic
  workflow. Telegram selects only opaque project/session/destination options;
  it never supplies a filesystem path. Discovery is limited to exact canonical
  registered roots and supported persisted interactive sources (`cli` and
  `vscode`), exposes no transcript or prompt text, and invokes no model, thread
  creation, or resume. Metadata discovery MUST progress while the Codex worker
  waits on an unrelated productive turn, using a separate SQLite connection and
  app-server client. If a pending discovery or activation recheck expires, Hub
  MUST durably notify the owner once; expiry MUST NOT silently leave a workflow
  with no result. Hub private free text MUST NOT become productive
  provider input. Local configuration MUST be explicit rather than discovered
  from hidden user files.

## 11. Frontends, writer lease, and local transfer

### Current behavior

- **REQ-WRITER-001 (Implemented):** One provider topic session MUST have only one
  active writer, and Hub-owned productive execution across all topic sessions
  MUST have at most one writer for the same canonical project root.
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
  integration. Hub-managed Codex turns on a shared app-server MUST be marked as
  approval-only for a compatible tlive companion: remote approvals remain
  available, while prompt/completion mirroring and reply-to-continue MUST stay
  disabled so tlive cannot become a second conversation writer outside the Hub
  queue and writer lease.

### Implemented minimal native transfer

- **REQ-WRITER-006 (Implemented):** `/local` validates that no Hub dispatch,
  unpaused queued/in-flight work, active or unconfirmed provider turn, or other
  local writer owns the same canonical root and that a provider session exists.
  For an accepted uncertain Codex turn, it MAY first read the exact saved turn
  without invocation; only confirmed terminality can permit write-capable
  transfer. Earlier queued work MUST remain paused for owner review. `/local`
  changes `writer_mode`
  from `telegram` to `local`, and returns a reviewed
  provider-specific resume command for the canonical root and session ID.
  Antigravity MUST use the configured executable and the same model/effort
  argument builder as productive turns. An explicit effort replaces a known
  existing effort suffix; default effort preserves the selected model ID.
  Named managed Codex profiles remain excluded from local/tmux transfer until
  its execution boundary is independently verified; refusal MUST precede
  provider preparation, process launch and lease mutation (REQ-SEC-001).
- **REQ-WRITER-007 (Implemented for Codex with explicit owner assertion):**
  after the owner closes the CLI and Hub work is terminal, `/return` changes
  only the lease; it invokes no model and copies no summary or transcript. The
  next Telegram turn resumes the same session. V1 does not infer OS process
  state. An explicit local reconciliation MAY adopt an already opened exact
  Codex session without launching a CLI or a model, only after matching active
  Hub session, provider thread, generation, origin and canonical root, proving
  the old turn terminal through read-only protocol, excluding other Hub writers
  and leases, and receiving an owner assertion that the standalone CLI has
  closed at an idle boundary or a remote CLI is idle. It MUST leave the old
  uncertain job and its checkpoint, error and notice intact. Process absence
  alone cannot establish this boundary. Other providers retain prior behavior
  pending separate acceptance.
- **REQ-WRITER-008 (Implemented):** Messages arriving while `local` owns the
  writer do not call a provider and explain how to return safely. A productive
  request from another numeric topic on the same canonical root MUST receive a
  durable input-bound refusal instead of entering the queue; the owner-topic
  link MUST use numeric identity and a neutral label unless a trustworthy title
  is available. It MUST say that the provider did not receive the request and
  that it will not run later. `/status` in owner context MUST explain the
  blocker and route to `/return` or `/release`; anonymous monitor aggregates
  MUST NOT expose topic or session identity. A local writer lease MUST NOT
  expire merely because it is old or has no visible PID. `/return` remains a
  model-free same-session lease change after the owner closes the CLI.
- **REQ-WRITER-009 (Implemented; live acceptance pending):** An explicit local
  `session attach-codex` preview/apply MAY connect a saved Codex thread to a
  registered topic. It MUST verify exact persisted metadata and the canonical
  allowlisted Git root without productive inference, transcript import,
  bot-token reads or implicit migration. Apply requires an explicit CLI-closed
  assertion and external Codex workers/external outbox. It atomically creates a
  fresh Hub identity and immutable origin with writer `local`. Replacement
  requires the exact previous active Codex session ID; busy or unresolved work,
  unfinished deliveries and known local writers on the canonical root, including
  known prior project registrations, block it. Only the old Hub
  binding is archived; idle satellites and history remain.
- **REQ-WRITER-010 (Implemented):** First `/return` on an adopted session MUST
  atomically persist the activation message ID, forwarded-context floor, writer
  change and receipt. Productive ingress at or before that boundary MUST be
  refused inside admission and batching transactions. Old quotes MUST NOT cross
  the boundary through delayed delivery. Explicit `/context` remains available.
  Stale controls MUST NOT mutate a replacement generation; old Reply routing
  addresses the current provider binding. Exact apply retries MUST NOT create
  another generation or reset ownership after return.
- **REQ-WRITER-011 (Implemented):** Adopted execution MUST validate origin/root
  before resume overrides and continue the exact provider thread, including on
  stdio fallback. Read/resume failure MUST NOT create a substitute conversation.
  Model/effort changes and provider switching preserve origin; explicit `/new`
  starts a normal new conversation while retaining the old origin reservation.
  Unsupported Codex execution modes MUST refuse retained origins, including
  archived bindings. CLI environment/plugin parity is not guaranteed.
- **REQ-WRITER-012 (Implemented; live Telegram acceptance pending):** A
  confirmed session-connect workflow MUST recheck the exact source metadata,
  destination binding, busy work and expected prior session before activation.
  The standalone sender MUST publish one neutral marker in the destination and
  use its positive Telegram message ID as the input/context boundary. One
  SQLite transaction then performs the existing immutable-origin attachment,
  replacement archive, activation, writer transfer to Telegram, one-time-code
  consumption, workflow completion, marker receipt, and preparation of the
  success result. Success MUST NOT be published before this commit. An unknown
  marker outcome MUST retain the old binding and MUST NOT be retried blindly.
  The next later ordinary message continues the exact chosen thread without a
  separate `/return`; stale callbacks, cancellation and expiry create no new
  generation.
- **REQ-WRITER-013 (Retired by ADR 0065):** Hub-managed lead/advisor permissions
  and role handover are withdrawn. Existing writer leases, canonical-root
  exclusion, sandbox and human approval authority remain mandatory. Project
  review instructions MUST NOT be treated as access isolation or approval
  grants. The ID remains reserved; see
  [ADR 0065](../decisions/0065-retire-hub-lead-advisor.md).

Initial reviewed resume shapes are `codex resume SESSION_ID -C ROOT`,
with explicit provider/model `-c` overrides when local `codex_model_provider`
is configured,
`opencode ROOT --session SESSION_ID`, and
`cd -- ROOT && agy --conversation SESSION_ID --sandbox --mode accept-edits --model MODEL_EFFORT`. They are version-sensitive
adapter capabilities, not permanent user-input templates. Hermes requires a
separate native capability check.

An explicit local Codex route MUST preserve the source provider as immutable
provenance while pinning the configured provider for execution and `/local`.
Discovery MUST accept only OpenAI and that exact configured provider. A route
mismatch MUST fail before productive inference; exact-session failure MUST NOT
be repaired by creating a replacement thread. Existing root, activation and
single-writer checks remain mandatory. See
[ADR 0028](../decisions/0028-explicit-codex-provider-routing.md).

## 12. Approval, sandbox, and secret requirements

- **REQ-SEC-001 (Implemented):** Codex MUST remain `workspace-write`.
  Companion-capable shared sockets use `on-request`; an isolated headless stdio
  fallback uses `never` so sandboxed work may proceed but escalation cannot be
  requested. Any unexpected server approval request on that fallback MUST be
  explicitly declined. `danger-full-access`, dangerous provider bypass flags,
  and automatic approval MUST be rejected.
  An explicit local `codex_permission_profile` MAY select a bounded named
  managed profile through external Codex queue workers. Hub MUST verify, through
  bounded passive metadata on the preparing connection, that the configured
  selection is the managed default and the sole allowed profile. Start/resume
  and turn submission MUST select that exact profile without legacy sandbox
  overrides, retaining the transport's approval policy and human reviewer.
  The session generation, accepted job, confirmed execution checkpoint and
  connect authorization MUST retain immutable selection snapshots. Missing
  configuration context MUST refuse Codex creation/admission/activation;
  explicit legacy `null` remains distinct. Existing legacy rows MUST stay
  legacy after migration. A configuration change MUST NOT retarget old work;
  an explicit `/new` MAY create a generation with the current selection.
  Confirmation MUST match the profile, canonical root, provider, approval
  policy and reviewer and reject visible network access, writable roots outside
  the project and implicit temporary writable roots. Managed parent metadata
  MAY explicitly be `null`; it is not proof of inheritance. Matching settings
  changes during preparation MUST be checked before turn submission. A later
  policy change MUST trigger a bounded interrupt and retain uncertain execution
  status until exact native terminality is separately proven.
  Managed exact resume MUST NOT substitute another thread. Managed steering,
  inline/pilot execution and local/tmux transfer MUST refuse before their effects
  until separately supported. Metadata proves selection continuity only; it
  does not expose the full managed definition or establish read isolation,
  authority-data custody or helper isolation. Those claims
  require independent OS-boundary and negative access evidence.
- **REQ-SEC-002 (Implemented):** Hermes and Hub are not approval authorities.
  Codex/tlive retains approval ownership and first-valid-answer-wins behavior.
- **REQ-SEC-003 (Accepted):** Timeout, restart, ambiguity, missing state, and
  channel failure MUST resolve to deny/no action, never approval.
  Neither provider MAY approve the other's actions; enforcement against
  provider/helper access to approval channels remains subject to REQ-SEC-008 custody.
- **REQ-SEC-004 (Implemented):** Tokens MUST live in private local files, not
  command arguments, JSON examples, logs, Git, documents, or Telegram content.
- **REQ-SEC-005 (Implemented):** State and secret files MUST use restrictive
  permissions; diagnostics MUST report unsafe permissions without printing
  secret values.
- **REQ-SEC-006 (Implemented):** Telegram owner, private-group, project-root,
  topic, and agent allowlists MUST be enforced before provider invocation.
- **REQ-SEC-007 (Accepted):** A provider failure MUST be visible, reversible,
  and isolated. Recovery MUST NOT weaken security policy to regain availability.
- **REQ-SEC-008 (Implemented offline; live acceptance pending):** An explicitly
  configured Claude file-tool turn MAY expose only Read, Glob, Grep, Write and
  Edit inside a fail-closed Linux filesystem/PID boundary. Private Hub state,
  bot/web credentials and permission signing keys MUST be absent from its
  mounts; trusted runtime and hook code MUST remain immutable to the provider.
  Only a fresh allowlisted human callback in the protected tlive namespace MAY
  authorize a prompted operation. The receipt MUST authenticate the exact
  payload, daemon epoch, nonce and original Telegram actor. Hub MUST recheck
  the live job, lease, writer, native session, generation, project/root and stop
  state, then atomically consume the receipt once before native Allow. Inputs
  requiring masking or an incomplete preview MUST be denied. Native permission
  suggestions MUST NOT become persistent grants or input changes. Restart,
  timeout, unavailable isolation/host and ambiguity MUST deny without replay.
  The per-turn hook MUST have no signing keys or Hub state access, and no Stop,
  continuation, mirroring or second conversation writer. Shell, MCP, skills,
  external plugins and children remain unavailable. This slice does not establish
  subscription routing or local-transfer parity.
  The trusted worker and tlive host remain the receipt trust base; this slice
  MUST NOT claim isolation from an unconfined hostile process sharing their UID.
  Live activation MUST exclude such principals from signing keys, Hub state and
  transport endpoints through independently verified OS isolation. Prompt-based
  review MUST NOT exempt helpers or other launch paths from this custody gate.

The detailed threat model in `docs/SECURITY.ru.md` remains normative where it is
more specific and consistent with this baseline.
