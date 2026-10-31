# Accounts, control, and security requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#accounts-control-and-security).

## 10. Codex accounts and transports

- **REQ-AUTH-001 (Implemented):** Work MUST use only owner-allowlisted accounts; discovered accounts
  grant no authority and identifiers MUST stay outside Git.
- **REQ-AUTH-002 (Implemented; ADR 0047):** Codex MUST use the official login through its configured
  shared socket or official stdio server. Retired multi-auth keys MUST fail loading by name before
  filesystem/helper access.
- **REQ-AUTH-003 (Retired by ADR 0047):** Hub MUST NOT access, execute, display or alert on Codex
  account pools; old pool state MUST be ignored or released.
- **REQ-AUTH-004 (Implemented):** Shared-server failure MUST allow official stdio fallback or
  visible Codex-only degradation. Fallback uses a new thread with bounded saved visible context,
  except adopted threads MUST resume exactly or refuse.
- **REQ-AUTH-005 (Implemented):** Account changes and quota state MUST be visible; switching MUST
  NOT be silent.
- **REQ-AUTH-006 (Accepted):** Hermes MAY guide manual, mode-aware login recovery without exposing
  tokens, changing the wrong store or bypassing deterministic locks and checks.
- **REQ-AUTH-007 (Retired by ADR 0047):** Codex account switching and its transition acceptance are
  withdrawn.
- **REQ-AUTH-008 (Retired by ADR 0047):** Rotating-server unit ordering is withdrawn; tlive and the
  shared server MUST remain independent.
- **REQ-AUTH-009 (Claude repository scaffold; live acceptance pending):** Claude MUST use one
  explicit authentication route, defaulting to CPA; ambiguous or mixed credentials/selectors MUST
  refuse before launch.
  - CPA MUST use explicit loopback routing and one client credential; deployment MUST establish no
    paid/API or extra-usage fallback, not merely a local URL.
  - Subscription MUST use one nonempty token from a private owner-created file, no CPA/API/cloud
    credentials and no developer profile; extra usage MUST be disabled before activation.
  - A project-bound account MUST fail rather than fall back, pool, rotate or switch on limits; other
    projects use the default account.
  - Claude MUST load no user/project/local settings and receive at most its selected token; other
    providers MUST receive none.
  - Text-only and denied permissions MUST remain the default; file tools require explicit opt-in and
    REQ-SEC-008 acceptance, without implicit customizations, skills or MCP.
  - Source, native and Telegram acceptance MUST remain distinct; the bounded developer milestone
    grants neither full parity nor authority-data custody.

### Compact control surface

- **REQ-CMD-001 (Implemented):** Status MUST show provider/model/effort and only observed context,
  masked account and limit/reset data; stale or missing values MUST remain unknown.
  - Context remainder MUST use current context, not lifetime usage; quota labels MUST use reported
    durations, never guessed windows.
  - A goal footer MAY report exact accepted-turn events as “observed,” preserving distinct states;
    it MUST NOT infer current completion, delivery-time state or fast mode.
  - Observation MUST add no calls or authority and MUST NOT disrupt mandatory processing; retries
    retain the saved footer and recovery cannot borrow later state.
- **REQ-CMD-002 (Implemented):** One cascaded model selector MUST show current choices and validate
  the displayed catalog snapshot; final selection MUST be local and model-free. Claude choices MUST
  come from bounded valid local configuration including the default, be labelled
  availability-unverified, and confer no entitlement, effort or billing proof.
- **REQ-CMD-002A (Implemented):** Discovery MUST preserve a private last-good catalog, bounded
  opaque paginated choices and cache-only Controller operation; failed refresh MUST retain choices
  and warn only after 24 hours without success. Claude refresh MUST be local-only, invalidate
  removed choices and retain a visibly selected obsolete choice without automatic reset; Codex
  discovery MUST use native metadata without threads, inference or account helpers.
- **REQ-CMD-003 (Implemented):** Accounts MUST use only private configuration and saved/passive
  telemetry, with masked identities and honest unknowns. Codex account lists MUST be refused,
  OpenCode exact exhaustion/reset requires observed rejection, and unsafe, stale or mismatched
  Antigravity caches MUST yield unknown.
- **REQ-CMD-004 (Implemented):** Confirmed `/new` MUST reset only the active session; `/local`
  transfers its writer and Codex `/return` changes only the lease. Codex local attachment MUST use
  the owning server with retained permissions and no competing writer; other providers keep bounded
  summaries pending acceptance.
- **REQ-CMD-005 (Implemented):** Menus MUST expose only supported controls: provider defaults are
  status/model/accounts/new/local/return/stop, project Hub menus are menu/connect/stop, and private
  Hub menus are start/projects/connect/cancel. Provider group menus stay empty; local
  synchronization checks every scope.
- **REQ-CMD-006 (Implemented):** Buttons MUST mark the active choice; account/quota summaries MUST
  use portable status symbols.
- **REQ-CMD-007 (Implemented for queued project-group providers):** Stop controls MUST bypass
  models, cancel not-started jobs and interrupt the running provider within the numeric topic,
  including satellites; held work and other topics remain untouched. Only complete recognized
  emergency utterances count, and owned CLI groups MUST remain force-stoppable; unsupported legacy
  endpoints MUST NOT advertise stop.
- **REQ-CMD-008 (Implemented; live Telegram acceptance pending):** Topic, owner-private and local
  session-connect entries MUST share one durable model-free workflow with opaque choices, explicit
  local configuration and exact registered-root discovery. Discovery MUST expose no transcript,
  remain responsive during unrelated work, and durably report expiry once; private control text MUST
  NOT become productive input.

## 11. Frontends, writer lease, and local transfer

### Current behavior

- **REQ-WRITER-001 (Implemented):** Each session MUST have one writer, and each canonical root at
  most one Hub-owned productive writer across topics.
- **REQ-WRITER-002 (Implemented for Codex tmux takeover):** Terminal takeover/release MUST transfer
  the writer between Telegram and named tmux without changing the thread.
- **REQ-WRITER-003 (Implemented):** Telegram MUST refuse productive input while the terminal owns
  the session.
- **REQ-WRITER-004 (Accepted):** tmux is a persistence/reattachment fallback, not the preferred rich
  interface.
- **REQ-WRITER-005 (Accepted):** tlive MUST claim semantic integration only for supported providers;
  Hub-managed shared-server Codex sessions MUST allow approvals without mirroring or reply
  continuation that creates a second writer.

### Implemented minimal native transfer

- **REQ-WRITER-006 (Implemented):** Local transfer MUST require an existing exact session and a root
  free of competing writers, dispatches, unpaused work and uncertain execution; read-only exact
  terminal proof MAY resolve Codex uncertainty while queued work stays held. The resume command MUST
  preserve validated root, provider and model/effort; unsupported managed-profile transfer MUST
  refuse before preparation, launch or lease change.
- **REQ-WRITER-007 (Implemented for Codex with explicit owner assertion):** Codex return MUST
  preserve the session without inference or transcript copying after terminal work and the owner's
  assertion that the CLI is closed. Exact local reconciliation additionally permits an idle remote
  CLI, but MUST verify all bindings and terminal proof, exclude competing writers and preserve
  uncertain history; process absence alone is insufficient.
- **REQ-WRITER-008 (Implemented):** Local ownership MUST durably refuse same-root input, explain
  that it was not sent and will not run later, and offer safe return controls. Leases MUST NOT
  expire through age or missing PID; private owner links use numeric identity and anonymous
  monitoring exposes no topic/session.
- **REQ-WRITER-009 (Implemented; live acceptance pending):** Local adoption MUST preview and verify
  the exact saved thread/root without inference, transcript import, tokens or migration; apply
  requires CLI-closed assertion and external workers/outbox. Replacement MUST atomically create a
  fresh Hub identity with immutable origin and local ownership, match the prior session and refuse
  busy, uncertain, delivery or known-writer conflicts while preserving history and idle satellites.
- **REQ-WRITER-010 (Implemented):** First return MUST atomically activate adopted ownership with an
  input/context boundary; earlier input, delayed quotes and stale controls MUST NOT affect the new
  generation. Repeated apply MUST NOT create generations or reset ownership; explicit context
  remains available and old Replies route to the current binding.
- **REQ-WRITER-011 (Implemented):** Adopted work MUST validate origin before overrides and resume
  the exact thread, including fallback; failures MUST NOT substitute conversations. Model/provider
  changes preserve origin, new sessions retain old reservations, and unsupported modes MUST refuse
  retained origins.
- **REQ-WRITER-012 (Implemented; live Telegram acceptance pending):** Connect MUST recheck source,
  destination, prior session and blockers, then atomically activate once behind a positively
  receipted destination marker before reporting success. Unknown marker delivery MUST preserve the
  old binding without blind retry; stale, cancelled or expired controls create no generation, and
  the next later message resumes the chosen thread directly.
- **REQ-WRITER-013 (Retired by ADR 0065):** Retired roles grant no permissions; writer/root
  exclusion, sandbox and human approval remain mandatory, and project review instructions MUST NOT
  imply isolation or approval.

Explicit Codex routes MUST preserve original provider provenance, pin execution/local
routing, and discover only OpenAI or the exact configured provider. Mismatch MUST
refuse before inference; exact-session failure MUST NOT create a replacement.

## 12. Approval, sandbox, and secret requirements

- **REQ-SEC-001 (Implemented):** Codex MUST retain workspace-write, human approval on companion
  sockets, and denied escalation on isolated stdio; bypass flags and automatic approval MUST be
  rejected.
  - An explicit managed profile MUST be passively verified as the sole allowed default and selected
    unchanged for start/resume/turns, without legacy overrides.
  - Session, job, checkpoint and connect authorization MUST pin the selection; missing context
    refuses work, legacy stays legacy, and configuration changes require a new generation.
  - Confirmation MUST match root/provider/profile/policy/reviewer and reject visible network,
    outside-project or implicit temporary writes; preparation changes require rechecking.
  - Later policy drift MUST trigger bounded interruption while retaining uncertainty until exact
    terminal proof; exact resume MUST NOT replace threads.
  - Unsupported managed steering, inline/pilot and local/tmux execution MUST refuse before effects;
    selection metadata MUST NOT claim read, authority-data or helper isolation.
- **REQ-SEC-002 (Implemented):** Hub and Hermes MUST NOT approve; Codex/tlive retains human approval
  and first-valid-answer-wins.
- **REQ-SEC-003 (Accepted):** Timeout, restart, ambiguity, missing state or channel loss MUST
  deny/no-op; providers MUST NOT approve one another. Human-only checks remain mandatory without
  claiming same-UID/admin tamper resistance.
- **REQ-SEC-004 (Implemented):** Tokens MUST stay in private local files, never arguments, examples,
  logs, Git, documents or Telegram.
- **REQ-SEC-005 (Implemented):** State/secrets MUST have restrictive permissions; diagnostics MUST
  report unsafe permissions without values.
- **REQ-SEC-006 (Implemented):** Owner, private-group, root, topic and agent allowlists MUST pass
  before invocation.
- **REQ-SEC-007 (Accepted):** Provider failure MUST be visible, reversible and isolated; recovery
  MUST NOT weaken security.
- **REQ-SEC-008 (Minimal developer accepted; temporary Bash consent and workstation live acceptance
  pending):** Claude tool access MUST follow an explicitly selected profile and verified launch
  boundary; failed confinement MUST refuse, never launch unconfined.
  - The editor profile permits project read/search and requires fresh human permission for each
    Write/Edit, with read-only ordinary Git metadata; editor acceptance MUST prove native
    enforcement, not merely manual settings.
  - Confined profiles MUST exclude private state, credentials and signing keys, retain immutable
    runtime/hooks and separate session HOME, and expose no MCP, skills, plugins or agent children.
  - Only fresh allowlisted human callbacks MAY authorize prompted actions; exact authenticated,
    single-use receipts MUST bind payload, actor, epoch and live
    job/lease/writer/session/generation/root/stop state.
  - Masked or incomplete previews, ambiguity, timeout, host/isolation loss or restart MUST deny
    without replay; suggestions/settings MUST NOT create grants or change inputs.
  - Human decisions MUST have a seven-day expiry bounded by the remaining live launch/channel;
    stop/disconnect/restart invalidates them, and total added launch time MUST NOT exceed seven days
    or replenish with requests/output.
  - The per-turn hook MUST have no keys, Hub state, Stop, continuation, mirroring or second-writer
    authority.
  - Timeouts and bounded cleanup MUST remain effective; waiting neither grants permission nor
    guarantees process suspension or official-hook origin.
  - Developer MAY preauthorize project edits and ordinary Git writes, but every Bash defaults to a
    fresh exact human decision.
  - Explicit owner-only “Bash until restart” MAY cover later turns only on the same
    worker/root/session/generation; the card MUST disclose that scope.
  - Temporary consent MUST stay in bounded memory, vanish on worker or daemon-epoch change, and deny
    if the host is unavailable; every reuse still requires current validation and a single-use
    journal entry.
  - Consent MUST NOT spread to other slots/tools/inputs or arise from Allow once, Deny, suggestions,
    settings or old receipts; cached consent MUST NOT claim human waiting.
  - Settled or late-expired cards MUST retire promptly; cosmetic failures MUST NOT delay, change or
    replay decisions, and local dispatch MUST NOT claim Telegram display.
  - Bash permission covers the command and descendants, not system-call review or hostile-project
    protection; background/bypass options, unknown fields and incomplete previews MUST refuse.
  - Profile and session HOME MUST stay pinned across exact resume/rollback; changes require a new
    session.
  - Toolchains MUST be bounded, trusted, read-only and separate from private
    authority/runtime/project/HOME; mutable tools, hardlinks and special files MUST refuse.
  - Trusted hook/control code MUST retain root-owned validation; launch-local Git settings MUST
    avoid automatic upstream/gc/maintenance without altering shared or sibling configuration.
  - An authorized text-only consultation MAY use one pinned route/model/effort with verified
    inference-only credentials and no paid fallback; no tools, sessions, writer, proxy, redirect or
    automatic retry.
  - Consultation MUST return complete visible text, retain uncertainty on
    timeout/mismatch/incompleteness and stop its owned caller without claiming remote cancellation;
    no management/signing credential may be exposed.
  - Linked worktrees MAY expose only their metadata plus admitted shared objects/refs/logs;
    pointers/config stay read-only and primary/sibling worktrees remain absent.
  - Whole common-directory mounts and unconfined administration fallback MUST refuse; confined
    project work storage stays hidden/read-only, with incoming/staging available.
  - Workstation explicitly exposes the host read-only, including credentials/authority, sockets and
    supplementary-group access; this accepts trusted-host risk, not isolation.
  - Its persistent writes MUST stay within project, admitted Git metadata and session HOME; private
    paths MUST NOT overlap writable/trusted mounts and inherited read-only mounts MUST stay locked.
  - Workstation preauthorizes validated Bash with current binding/stop checks and single-use journal
    entries, without fabricated human receipts or wait notices; push/deploy/service changes remain
    owner decisions by policy.
  - Developer acceptance MUST prove native edit/test/local-Git, fresh Bash Allow/Deny, consent
    reuse/reset/refusal, absent denied effects, exact resume and stop/cleanup/uncertainty,
    separately from installed Telegram acceptance.
  - The installation trusts one owner and assigned administrative/recovery helpers; MUST NOT claim
    hostile same-UID/admin isolation, prompt-injection resistance, or exfiltration isolation from
    permissions, metadata or shared networking.
  - Scoped confined-launch acceptance MUST identify reachable authority endpoints and prove private
    files/FDs, signing, management and host control require unavailable secrets; CPA credentials
    MUST grant no management/host control and unknown exposure MUST refuse activation.
  - Those checks are point-in-time, not host-wide guarantees; stronger isolation and full provider
    parity MUST NOT become extra gates for this bounded trusted installation.
  - Backups recover captured data only, never disclosures, external effects or forged authority.

The [security threat model](../SECURITY.ru.md) remains normative where more specific
and consistent with this baseline.
