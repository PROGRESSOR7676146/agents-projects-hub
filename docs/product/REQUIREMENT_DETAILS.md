# Requirement details

Non-normative reference, excluded from the manifest. Linked modules own the outcomes;
these notes retain moved mechanics, edge cases and history, not implementation proof.
Entries use originating IDs; unnumbered context stays with its module. IDs without
entries have no additional detail.

## PRODUCT REQUIREMENTS

Source: [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md).

Section context: writer modes `telegram/local/tmux`; original terminology called
local ownership “future”, section11 records implementation. Agent Session Remote
renames tlive, a private control chat. Package `hermes_codex_router` retained.
Multi-auth retirement ADR0047; roles ADR0065. Durable input does not imply full
transcript mirroring or portable in-flight work.

## IDENTITY AND INTERACTION

Source: [IDENTITY_AND_INTERACTION.md](IDENTITY_AND_INTERACTION.md).

Section 6 examples: `example-project`, `/home/example/projects/example-project`,
“Example Project”; `disabled-example`, `/home/example/projects/disabled-example`,
“Disabled Example”. Registry root checks use configured `allowed_root`.

### REQ-ROUTE-005

Hermes native Gateway owns Telegram/session; Hub plugin/hook checks admission/exchange.

### REQ-ROUTE-006

Precedence: real known-bot Reply, explicit mention, passive forward, selected/pasted
quote, ordinary input. Protocol reply to topic root is only an anchor. Modern/
legacy forward metadata bypasses command/stop/mention/productive parsing.

### REQ-ROUTE-009

Batch key: admitted user + numeric topic + agent/session/model/effort. Quiet window
collects separately saved inputs; commands, explicit Reply/mention route change,
maximum window or payload cap close it. Unaddressed continuation inherits satellite
target without switching active agent.

### REQ-ROUTE-010

Socket Codex uses `turn/steer`; rejection returns FIFO, ambiguity is
`indeterminate`. Opt-in Claude accepts one streamed correction with exact
receipts; other runtimes retain FIFO.

### REQ-UX-003

Private admission/external refresh reuse one ephemeral `Thinking…` draft ID;
groups use `typing` until Bot API group-draft support.

### REQ-UX-003A

`readBusinessMessage` is connected-business-only, not ordinary bot read receipts.

### REQ-UX-004

HTML-aware ordered splitter; queued turns persist bounded parts/message IDs and
resume first unreceipted part. Immediate legacy paths share splitting, not durability.

### REQ-UX-005

Exact staging: `.hub/staging/<job_id>`. Checks: canonical regular file, per-file/
aggregate bytes, extension, safe filename, secret-like name. Private immutable
spool binds path/size/SHA-256/display name; revalidated at send, cleaned after
acceptance. Shared staging, symlinks, archives and prose paths do not authorize.

### REQ-UX-009

Captions, UTF-8 documents, supported images, selected quotes and album parts bind
project/root/session generation/FIFO. File-only input gets neutral instruction.
Same media-group album includes a later synchronous download extending past quiet
window; post-start material is later FIFO, never text steering. Bot API cap 20 MiB
per file, unaffected by Premium. Unsupported/excess/unavailable/legacy content
reports absence, not metadata-as-reading.

Claude default-false image gate: bounded PNG/JPEG through owned external CLI, tools
off, incompatible with protected file tools. Original bytes match spool. One
processed-input receipt binds current caller UUID/exact native session and prompt/
markers/image order/count, retaining bounded inline supported images. Processing
may alter bytes or convert PNG→JPEG; missing/conflicting/omitted/image-to-text
receipt after spawn retains material/uncertainty/root exclusion.
[ADR 0066](../decisions/0066-verified-claude-image-input.md).

### REQ-COLLAB-002

ADR0065 withdraws sequential advisor-review/lead-continuation.

Section9 provider mechanics: Codex persistent app-server/usage; Hermes Gateway;
OpenCode Go-authenticated structured CLI; Antigravity `agy` sandboxed
`accept-edits`. Original Claude table: protected-editor source, personal developer
accepted (ADR0075), implementation/live parity open; explicit CPA/subscription
(ADR0082), tools separate. Gemini CLI rejected in favor of Antigravity absent new
decision. Bots represent runtimes.

## ACCOUNTS CONTROL AND SECURITY

Source: [ACCOUNTS_CONTROL_AND_SECURITY.md](ACCOUNTS_CONTROL_AND_SECURITY.md).

### REQ-AUTH-002

Retired keys: `codex_multi_auth_dir`, `codex_multi_auth_executable`,
`codex_account_hints`; fail by name before filesystem/helper access (ADR 0047).

### REQ-AUTH-004

A connection-refusing socket is unhealthy despite presence. Select configured
official stdio before turn start. Ordinary fallback uses fresh thread/bounded saved
visible context because shared server may retain old writer; adopted threads resume
exactly or refuse.

### REQ-AUTH-009

`auth_route` defaults `cpa`: loopback `ANTHROPIC_BASE_URL`, exactly one client
credential, no cloud selectors/subscription token/ambiguous bearer-API mix.
Private route verification excludes paid/API/extra usage; local URL is no billing
proof. Subscription uses private owner-created `CLAUDE_CODE_OAUTH_TOKEN`, no
CPA URL/bearer/API/cloud selectors or developer profile; extra usage off before
activation. Fixed project token has no fallback; others use default. No user/
project/local settings; at most chosen token for Claude, none for other runtimes.
[ADR 0082](../decisions/0082-claude-subscription-route.md).

Default exposes no built-ins/customizations/skills/MCP and denies prompts.
File tools opt in under REQ-SEC-008. ADR 0067 retains editor gates; ADR 0075 minimal
ordinary-root developer implementation/scoped acceptance was complete. Later
steer/extensions, native transfer/images and personal chats are separate, not
prerequisites; acceptance never changes text-only default.

### REQ-CMD-001

Status: provider/model/effort/current context/masked reported account/limit-reset.
Current-context snapshot, not lifetime usage; missing clears old. Window labels
use reported duration; unknown primary/secondary stay named by position. Missing
account window may use same-turn rolling rate-limit event only for that response.
Goal footer: payload-free exact accepted thread/turn events; active/paused/blocked/
usage-limited/budget-limited/complete, labelled observed (latest during submit/wait,
not completion/delivery time). Foreign/unbound/clear/malformed/exhausted/new-submit
evidence cannot create modes; unknown omitted, not off. No extra RPC/inference/
authority; saved footer on retry, no current-thread borrowing on recovery.
Prompt/preparatory/subsequent service tier does not prove current `/fast`.

### REQ-CMD-002

Final cascade click validates displayed cached snapshot, no new RPC/handoff.
Claude catalog: 1–32 unique ASCII IDs ≤128 chars, printable labels 1–96, unique
nonempty efforts from low/medium/high/xhigh/max. Only model_id/label/efforts;
default pair included, omission keeps one default; other runtimes reject field.
Configured choice proves no availability/entitlement/effective effort/billing.

### REQ-CMD-002A

Private atomic last-good cache: source version/time, opaque bounded keys/pagination.
Failure warns only when last success >24h. Monitor native Codex `model/list` on
configured socket replaces other-source cache, with no threads/inference/helper.
Controller Refresh requests monitor; only empty cache uses default. Claude display/
callbacks/refresh use current config locally, invalidating removed choices despite
fresh cache; obsolete selection remains visible without reset/replacement.

### REQ-CMD-003

Local config/state only. OpenCode Go exact reset/exhaustion follows real 429; caps
labelled separately. Codex in `provider_account_hints` fails; other providers use
short masked prefixes, including exhaustion of unknown account. Optional private
Antigravity structured cache: account/per-model quota/reset/model-effort/matching-
session context, no ANSI parsing. Stale/mismatch/oversize/unsafe becomes unknown.

### REQ-CMD-004

Owner-confirmed `/new` resets active session only. Codex TUI uses `--remote`
owning Unix socket, no permission overrides/competing standalone persistence.
Return is lease-only; other providers retain summaries pending acceptance.

### REQ-CMD-005

Default: `/status /model /accounts /new /local /return /stop`.
Group Hub: `/menu /connect /stop`; provider scoped menus empty (no username-suffix
duplicates). Provider DM: supported commands only. Hub private:
`/start /projects /connect /cancel`. Legacy maintenance remains local, not mobile.
Deterministic local synchronization checks all scopes.

### REQ-CMD-006

Telegram success button style where supported; green/yellow/red status symbols
because text color is not reliably portable.

### REQ-CMD-007

Exact normalized case-insensitive utterances: stop, halt, стоп, стой, остановись,
прекрати; embedded prose never matches. Topic-wide unstarted cancellation and
one running native/process-group interruption include satellites, preserve holds.
Ignored graceful termination can be forced. Legacy inline OpenCode/Antigravity DMs
do not advertise stop until interruptible workers.

### REQ-CMD-008

Entries: topic/private `/connect`, local `session connect [CONFIG]`.
Discovery sources `cli/vscode`, exact-root metadata only. Separate SQLite/app-server
clients allow unrelated busy-worker discovery. Discovery/activation expiry notifies
once; local config explicit, not hidden-file discovery.

### REQ-WRITER-002

Named tmux `/terminal` takeover; `/release` returns unchanged thread.

### REQ-WRITER-005

Supported semantic tlive: Codex/Claude. Hub Codex marker is approval-only:
no prompt/completion mirroring or reply continuation.

### REQ-WRITER-006

Checks include dispatch, unpaused queue/in-flight, active/unknown turn, local writer
and existing session. Exact Codex read-only terminal proof can clear uncertainty;
tails stay paused. Antigravity uses configured executable/productive model builder:
explicit effort replaces known suffix, default preserves model. Managed Codex local/
tmux refuses before preparation/launch/lease.

Historical reviewed adapter shapes (version-sensitive, not input templates):
`codex resume SESSION_ID -C ROOT` with configured provider/model `-c` overrides;
`opencode ROOT --session SESSION_ID`;
`cd -- ROOT && agy --conversation SESSION_ID --sandbox --mode accept-edits --model MODEL_EFFORT`.
Hermes needs its own native capability check.

### REQ-WRITER-007

V1 infers no OS process state. Exact reconciliation matches session/thread/generation/
origin/root and terminal proof; standalone closed at idle or remote CLI idle.
No CLI/model launch; old job/checkpoint/error/notice retained.

### REQ-WRITER-008

Refusal binds Telegram message. Numeric owner-topic links use neutral labels unless
titles are trustworthy; owner status offers return/release, anonymous monitor no IDs.

### REQ-WRITER-009

`session attach-codex` preview/apply reads persisted metadata without transcript,
bot tokens or migration. CLI-closed assertion + external Codex/outbox required.
Atomic fresh Hub identity/immutable origin/local writer; replacement pins prior
active Codex ID and checks same-root prior registrations. Only old binding archives;
idle satellites/history remain.

### REQ-WRITER-010

First return atomically saves activation message ID/context floor/writer/receipt.
Admission/batching reject input ≤boundary, including delayed quotes. Old Replies
route current provider; stale controls/repeated apply cannot mutate new generation.
Explicit context remains.

### REQ-WRITER-011

Origin validation precedes overrides, including stdio. New starts normal session
but keeps origin reservation; unsupported modes refuse even archived origins.
CLI environment/plugin parity unproved. Section-11 route: `codex_model_provider`
pins execution/local resume, discovers only OpenAI + exact configured provider,
retains source provenance; mismatch refuses before inference.
[ADR 0028](../decisions/0028-explicit-codex-provider-routing.md).

### REQ-WRITER-012

Recheck source/destination/busy/prior session. Standalone sender's positive neutral
marker ID becomes boundary. One SQLite transaction: attach/archive/activate/writer
to Telegram/code consumption/workflow completion/marker receipt/success preparation.
Unknown marker leaves old binding; cancel/expiry/stale controls create nothing;
next later message directly resumes.

### REQ-SEC-001

Legacy: workspace-write; companion socket on-request, isolated stdio never with
explicit decline of unexpected requests. Managed `codex_permission_profile` only
external queue: passive preparing-connection metadata proves sole allowed managed
default; exact selection on start/resume/submit with no legacy overrides.
Generation/job/checkpoint/connect snapshot it; missing context refuses, explicit
legacy null distinct and migration stays legacy. Confirmation pins root/provider/
profile/policy/reviewer, rejects visible network/outside-project/implicit temp writes.
Null parent proves no inheritance. Preparation changes rechecked; later drift
interrupts boundedly but stays uncertain until exact terminal. Managed steering/
inline/pilot/local/tmux unsupported before effects. Metadata proves continuity,
not full definition/read/authority/helper isolation; no extra host-wide gate.

### REQ-SEC-008

Native mechanics: Linux filesystem/PID namespace, editor Read/Glob/Grep/Write/Edit,
fresh human writes, ordinary Git read-only. Receipt binds payload, tlive epoch,
nonce, original Telegram actor; consume atomically before native Allow after current
job/lease/writer/session/generation/root/stop recheck. Offline candidate/manual mode
is not native enforcement proof.

Waiting: hook/transport support seven-day decision window. Validated journal-prepared
host wait adds at most seven days TOTAL per launch, no replenishment by requests/
output; remaining launch budget can end later requests early. Socket binding cannot
authenticate official hook: confined code can consume credit. Authorized descendants
may continue, no suspension claim.

Developer temporary consent is bounded worker memory tied to state/protected
transport, same worker/root/session/generation across turns. Every reuse checks
current tlive epoch, validates input/current authority and journals once; changed
epoch erases, unavailable host denies. Card settlement immediately removes buttons/
names decision; late receipts after timeout/disconnect also retire. Cosmetic failure
does not affect decisions; dispatch is not confirmed display. Bash includes tests/
Git-hook descendants, not per-syscall/hostile-project review. Profile/HOME pin across
resume/rollback; no MCP/skills/plugins/children.

Toolchain: bounded readonly root/worker-owned tools, PATH, visible Git-hook/Python/
identity settings; pinned native-filesystem/tree checks, no others-writable tools,
hardlinks/special files or overlap with private authority/runtime/project/HOME.
Control/hooks follow REQ-SEC-008. Launch-local Git disables automatic upstream/
gc/maintenance without shared/sibling config. Authorized separate consultation CLI:
one bounded question at pinned CPA route/model/effort/response model, no tools/
session/writer/proxy/redirect/retry/paid fallback; complete text only. Stop ends
caller, not proved remote cancellation. Prior inference-only/no-paid-fallback
verification, not declaration of billing/quota isolation.
[ADR 0078](../decisions/0078-claude-developer-tools-and-consultations.md).

Linked Git: own metadata+shared objects/refs/existing reflogs, profile access;
pointers/config readonly, primary checkout/index/HEAD/siblings absent, no wholesale
common-dir mount. Unsupported admin/layout refuses. Confined `.hub/work` hidden
by empty readonly mount, incoming/staging available.
[ADR 0076](../decisions/0076-confined-linked-worktree-git.md).

Workstation source/live-pending: host readonly, writes project/admitted Git/session
HOME at host path. Missing `.hub/work` created, readonly, not hidden. Namespace
`/dev/proc/tmp`, host `/run` readonly preserves resolver/host sockets incl Docker/
supplementary groups; host `/tmp` sockets absent. Permission socket
`/tmp/hub-permission.sock`. Nested userns/writable proc user maps permit
commit gates with inherited mounts locked readonly. Each Bash validates fields/
bindings/stop and single-use Allow journal, without human receipt/wait.
[ADR 0081](../decisions/0081-claude-workstation-host-view.md),
[0091](../decisions/0091-workstation-project-not-trust-scanned.md).

Trust: worker/tlive receipt base; same-UID/admin can read keys/state, forge symmetric
receipts, interfere. Unconfined Codex/OpenCode/Antigravity/native Hermes share this
accepted operational risk. Scoped point-in-time confined listener checks require
unavailable secrets for files/FDs/signing/management/host control; CPA inference
credentials grant no management. Developer production native/edit/test/Git and
grant/denial/resume/stop evidence is separate from installed human Telegram,
subscription and local-transfer parity.
[ADR 0072](../decisions/0072-trusted-personal-installation.md).
The [security model](../SECURITY.ru.md) remains normative where consistent/more specific.

## PERSISTENCE AND RECOVERY

Source: [PERSISTENCE_AND_RECOVERY.md](PERSISTENCE_AND_RECOVERY.md).

### REQ-OPS-004

Aggregates: accepted/delivered/partial/uncertain/recovered counts; pending queue/
final/progress counts/ages; unknown final/progress separate from pending/retry.
Owner disposition retains historical totals, separates outstanding/released holds;
only outstanding finals block alerts. Nonblocking unknown notices have separate
counts/deduplicated warning. No content or session/project/topic/account IDs.
Thresholds: expired execution lease; due unblocked queue >15 minutes without active
lease; committed final/progress >5 minutes; every newly unresolved uncertainty.
Old timestamps alone do not flag healthy execution; resolved history stays in totals.

### REQ-OPS-005

Fresh local heartbeat/status omits URLs/tokens. Approval comparison: exact persisted
thread/turn/typed request/item versus bounded daemon snapshot. Only adapter-positive
delivery reference proves sent; queued/stale/ambiguous/truncated/restarted evidence
does not. New waits have 30-second diagnostic grace, then one alert episode.
Attach-only custody rejects active/unknown work, unsettled controls, unowned socket/
server and artifact mismatch; no kill/unlink/general-diagnostic idle inference.
Archived-session exact owner resolution may close historical job for admission;
unarchived unresolved needs native proof. Full-control final consent removes only
its delivery gate, never other sender/local writer. Operator custody/source/
rollback identity/live acceptance stay separate.

### REQ-OPS-006

Operations requires Hub bot; Hermes fallback same topic. Unevaluated/retired episodes
release next notifying cycle. First two failures diagnostic, third required-component
error/degradation; successful request recovers only after threshold crossing.

### REQ-OPS-009

SQLite health fields: package version, exact Git SHA, build time, clean-tree assertion.
Excluded detail includes exception text, argv, paths, environment and account IDs.

### REQ-OPS-010

Manifest: active/rollback clean wheel identities and SHA-256, config digest,
consistent backup digest/schema, target schema. Re-inspect both wheels; candidate
hash-locked runtime/E2E export rejects staleness. Preparation is read-only.

### REQ-OPS-011

Temporary root contains state/config/releases/manifest/backup/activation pointers.
Schema20→candidate target, then distinct compatible rollback on retained target;
queued/prepared outbox/indeterminate rows unchanged. No live-state/network/service.

### REQ-OPS-012

Recovery captures artifacts, SQLite, project/provider stores, spool/history/config.
Verify digests/integrity/schema/stores/spool/uncertainty before consumers/network.
External infrastructure owns host/WSL export/shutdown/scheduling/storage/key admin/
cold drills; off-machine storage and separately held keys are prerequisites.

### REQ-OPS-013

Atomic journal each cycle before delivery: opaque ID/code/severity/bounded
Operations text/opened/last-seen/resolved/journal-updated times. Bounded, no content/
paths/credential/account/project/topic/Telegram identity or rollback migration.
Every code has catalog options/consequences/recommendation.

### REQ-OPS-014

Cards quote trigger/options/consequences/recommendation/Analyze/Dismiss; stale
journal has own card, missing cursor cold-start summarizes open episodes once.
Default-off notices-only increment omits functional Analyze pending native-entry
proof. Config pins numeric owner/private destination, Gateway periodic lifecycle
and private cursor; unsupported lifecycle refuses, config changes retire controls.
Cursor persists attempt before send; exact destination/positive receipt proves
delivery. Only proven rejection bounded-retries; unsafe existing evidence retained/
refused, absent cursor alone cold-seeds. Dismiss binds owner/chat/opaque token/
delivered message. Shutdown joins callbacks/file work before reconnect.

### REQ-OPS-015

Immutable version dirs, switchable links, private stack manifest; doctor/monitor
passive drift. Independent read-only plan→stage→check→dependency-order switch,
Hermes last, one recorded rollback. Control expires on gateway restart. Watchdog
restores previous Hermes if unit/heartbeat fails; Hub retains REQ-OPS-010/011.

Section context: Git/Telegram cannot recreate unsaved native context; old queue
subsection redirects to section22.

## Queue, control and delivery

Durable queue, control and delivery details are in
[their own reference](REQUIREMENT_DETAILS_QUEUE.md).

## ONBOARDING AND ACCEPTANCE

Source: [ONBOARDING_AND_ACCEPTANCE.md](ONBOARDING_AND_ACCEPTANCE.md).

### REQ-ONBOARD-001

Safe ID + opaque allowed root derives direct child. Separators/dot/callback paths/
forwards confer no authority; registry stores ID/name/Git root/allowed boundary.

### REQ-ONBOARD-002

Discovery: bounded numeric/title metadata. Static/dynamic registry resolution is
shared by ingress/execution/recovery/connect/audit using real Git toplevel checks.
DM adds exact configured project/owner chat; list errors stay per-group.

### REQ-ONBOARD-003

Confirmed-owner snapshot: creator remains creator, other owners invited/admin-granted/
verified before bots. External providers excluded from preflight/invite/promote/
verify. Exact active bot membership skips invite; only `UserNotParticipant` proves
absence, not lookup failure/left/banned/mismatch. Manage Topics optional for General.

### REQ-ONBOARD-006

MTProto creation/invite uses separate opt-in pinned-owner session/API hash/private
sidecars, not acceptance actor. Nonblocking process lock covers login/worker life;
bounded mutation RPC with client retry/reconnect off.

### REQ-ONBOARD-007

Idempotent root; proven rejection resumes only exact workflow/group/fresh owners,
reserving ID/root. One bot/group API per leased command step; per-bot cooldown
covers new bindings. Exhaustion explicit local reset. Command audit existing-only/
read-only/no migration. Completion without positive ID remains unknown.

### REQ-PROJECT-EDIT-002

Only `<allowed_root>/<immutable project_id>` may be initialized from opaque local options.

### REQ-PROJECT-EDIT-004

Registry projection/topic scope/dynamic binding serialize; handled fault rollback,
post-registry-replace crash recovery before ingress, identical callback repeat.

### AC-F-010

Fictional Controller/worker/sender subprocess fault matrix kills at durable
boundaries with no provider/Telegram network; separate live cutover acceptance.

### AC-F-011

Actor pins group/topic; only actor/Hub/configured provider senders accepted, first
failure aborts. Optional Codex P0/P1 requires explicit restart, private DB, aligned
response identity and initially active fixed Controller/Codex-worker units, restored
afterward. Seven results: captions, album unity, active-work FIFO, exactly-once
restart, over20MB notice, context/quota labels, read-only status/accounts. No
configured service names/productive prompts. Telegram does not deliver bot-authored
messages to bots, so bots cannot impersonate owner.

### AC-F-012

Temporary production-shaped schema20→candidate target, pointer candidate/back,
distinct compatible clean wheels opening retained queue/outbox/indeterminate state.

### AC-F-013

Empty/occupied/new destinations; expired/repeated/concurrent codes, stale callbacks,
changed targets, delayed input, restart, metadata faults, unknown send/create all
preserve exact thread and atomic old-binding archive. Marker activates once.

### AC-F-015

ADR0067 defers transfer/discovery/images from bounded-editor gates; retired role
acceptance is removed, not passed, and journal acceptance remains independent.

Section17 notes: schema33 snapshots/Codex `localImage`/legacy visible refusal;
queue durable versus legacy/DM immediate artifacts; schema28 provisioning/29 editing.
Inline default; embedded queue opt-in. External agents/outbox separately gated,
Controller delivery rollback default. Root capacity≤16, live rollout separate.
Original Claude scaffold/live-pending row retained separate human/scoped/subscription
gates, no global same-UID isolation and deferred parity.

## MAINTENANCE

Source: [MAINTENANCE.md](MAINTENANCE.md).

Section18: CLI flags vary; OpenCode/Antigravity tmux/PTY lack semantic companion;
Hermes transfer/Antigravity headless rotation need capabilities. Claude file boundary
offline evidence is distinct from installed/native/Telegram/CPA/subscription gates.

Section19: manifest generated integrity, not another norm; status lifecycle evidence,
runbooks procedures, testing guide commands/evidence, ADR durable rationale,
changelog release links. Canonical cheap checks before typing/tests; installed
pre-push clean-commit run is final local gate, not duplicate manual run; CI own head.
Passive data examples: quota-cache.json/logs/headers, no automatic --live.

Review triggers: roughly>1500-line file new responsibility, >200-line function
new material branch/lifecycle, duplicated lifecycle/invariant. Extraction plan,
not metric-only splitting. Exception rationale/owner/scope/review date/reopening
event. hotspots.json ratchets new/growth; raised bound in same change is reviewed
exception. PR ownership: component/transaction/cleanup, invocation boundary,
replay/failure certainty, focused/exact publication evidence. Neutral contracts/
domain helpers can support facades/orchestration; lower layers cannot import
Controller/provider-worker/Telegram. Other metrics trend/review only.
Pre-wave hotspots/exceptions/unclosed plans/worktree-branch-tag debt; plan state/
revision/trigger/closure; worktree owner/purpose/base/postmerge and tracked/staged/
untracked inspection. Section20 excludes private conversations/config/rollout data.

## EVALUATION AND ALLOCATION

Source: [EVALUATION_AND_ALLOCATION.md](EVALUATION_AND_ALLOCATION.md).

### REQ-EVAL-004

Optional bounded classifications/rubrics/recommendations; Jev is not required.

### REQ-EVAL-006

Versioned eligible ranking considers quality/time/total cost/scarcity; records
selected/rejected/deferred candidates, never broadens delegated executor selection.

### REQ-EVAL-007

Unknown use is bounded by conservative reservations or call/time limits;
otherwise authorized rotation still shares workflow budget/account policy.

### REQ-EVAL-009

Record types: task/attempt/assessment/resource/allocation, with correction and
delayed-outcome ownership. Aggregates use retained eligible records.

### REQ-EVAL-010

Schema45 fields: task/participant-model-effort/result-artifact/accepted-rework-unknown
reason/observed time-usage. Reserve non-forwarded
`/assess accepted|rework|unknown REASON` before Reply/mention/session/material.
Configured owner/registered topic/external queue-outbox. Any part can target final,
but every part incl documents needs strict version1 receipt; historical generation valid.
Fingerprint before bounded parsing includes canonical original-message digest before
quote/material normalization, excludes update_id. Irrelevant differences may conflict.
Corrections Reply latest applied human command, not bot ack; one successor.
HubState atomic four effects: disposition/input receipt/independent Hub ack/command
boundary advancing queued future deadlines. Exact duplicate cannot close later batch;
failed commit all four control-only retryable. Unsupported/scoped actor may refuse
once best-effort; noncentral poller cannot consume shared receipt; private controls
bypass onboarding/edit/connect.

Acceptance detail: duplicate outcomes/delayed corrections/attribution/shared pools/
stale-missing telemetry/reservations/pins/cold profiles/evaluator outage/manipulated
content/replay; journal duplicate-conflict/corrections/Reply/atomicity/no fallback
independent of AC-F015. Zero passive inference/unapproved exploration; paired
candidates cannot both publish/integrate. Comparative savings include fixed baseline,
overhead/quality/uncertainty/failed-discarded work; assignment gaps defeat causal
claims. [Research](../research/AGENT_SCORING_AND_ALLOCATION.ru.md) metrics/statistics/
Jev do not establish formulas/thresholds/experiments. Deployment evidence separate.
