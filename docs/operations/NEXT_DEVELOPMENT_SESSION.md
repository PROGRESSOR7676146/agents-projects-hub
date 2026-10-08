# Next session: Claude parity and visible task states

Status: native Claude and schema-37 visibility merged; human approval boundary in progress.
Date: 2026-10-04.
Decision owner: repository owner. Integration owner: lead development agent.
Last verified integrated revision: `3750ccfb0f8eb98333f9219f3a697d6328890d04`.
The stop/native/visibility stack passed exact-candidate independent reviews;
the merged revision passed canonical validation (1,488 tests in 135 modules,
no typing errors), hosted Python 3.11/3.12/3.13 validation and CodeQL.
This is repository evidence; deployment and live acceptance remain separate.
The protected file-tool candidate now has a pinned tlive extension, native hook,
schema-38 receipt journal and worker-owned socket/namespace wiring; see
[ADR 0052](../decisions/0052-protected-claude-file-permissions.md) and the
[candidate runbook](CLAUDE_FILE_PERMISSIONS.md). Text-only remains the default.
The authority-isolation source candidate at
`6a848c7a326b35e4c3ea552339e8c5c3d7beba53` passed canonical validation
(1,613 tests in 149 modules), independent review and hosted checks, including
both required real namespace scenarios; it remains unmerged and undeployed.
Next trigger: assess the existing narrow Claude boundary and actual agent
launch/service exposure, then choose the smallest enforceable custody candidate in
[ADR 0053](../decisions/0053-claude-custody-reference-deployment.md) and its
[runbook](CLAUDE_CUSTODY.md). A dedicated VM is a reserve option requiring a
demonstrated need; a separate UID is not yet accepted or proven. Preserve the
adversarial corpus and mandatory CI checks, and reuse completed mount, peer,
runtime and receipt controls. Then separately authorize and verify the
native/Telegram human approval boundary described in the
[Claude plan](CLAUDE_LEAD_REVIEW_PLAN.ru.md#следующая-граница-разрешений), then
continue advisor isolation and remaining visibility work. Do not enable tools
from a CLI flag or an unqualified tlive `allow` alone.
The current owner instruction caps helpers at GPT-6 Sol with reasoning `high`, using standard service without priority.

Current control-loss continuation: Stage 1 is published at
`fe7bb51ffa8dc491a33c624614dcff57ab40208d`; schema48 Stage 2 is published at
`b9822a797572ee6edab0d3b3c515516e38a68148`, unmerged, with canonical 2,581 tests
in 233 modules, Pyright zero errors, independent Astra/actual Opus sign-off and
seven exact-revision hosted checks. Its actual native offline control-loss
witness passed; none of this is deployment evidence. Do not repeat completed
publication gates after an interrupted agent turn.
Stage3's schema49 fenced group-poll ledger and pure freshness/grace policy are
published at `7af0204a117e2b1a23c0a364f44249c12de14fa4`, with canonical
2,628 tests in 237 modules, Pyright zero errors and seven hosted checks.
Schema50 immutable admission/accepted-target ingress provenance is published at
`4727241b3906c27ab4556cd4abd8eb6157e0c4f6`; its exact-revision gates are recorded
below. Schema51 durable continuity/episodes are under source validation.
Automatic precautions are not activated. Next is exact
commentary unknown delivery and full-control consent checked in the send-fence
transaction, then shared live/independent maintenance regression coverage.
Use immutable job-ingress sidecars and fresh-acceptance target rows; do not infer
authority from legacy jobs or observer labels. Retry children bind their current
Reply ingress; mixed/unknown provenance must not batch or steer into a proven
target. Late precautions share the existing three-read budget and permanent
fence without synthetic stop receipts. Stop binding must never move the next
late-read deadline backward. Queue/control clauses now have one owning module,
[section 22](../product/DURABLE_QUEUE_AND_CONTROL.md); their IDs are retained.
The remaining Claude/operations goal stays open. See
[control-loss acceptance](CODEX_CONTROL_LOSS.md) and the
[stabilization plan](STABILIZATION_PLAN.md). No live activation is authorized.

The Codex compatibility candidate now refuses unsupported active permission
profiles reported by `thread/start` or `thread/resume` before productive
`turn/start`, with a bounded durable queue notice. Absent/null profiles remain
compatible; this guard cannot detect a profile already lost during resume or
later control-plane changes.
Next implement narrowly explicit managed-profile support; profile ID and
`extends` alone cannot establish equivalent or stricter permissions. Keep
custody and native/Telegram acceptance open; this refusal is not profile support.
The optional [offline native profile corpus](../testing/README.md#optional-offline-native-codex-profile-rehearsal)
now makes the access-denial and legacy-override experiments reproducible with
fictional fixtures. Current native metadata exposes profile selection and
allowlisting, but not the managed profile definition. Resolve trustworthy effective
policy evidence before enabling profile support; meanwhile continue the remaining
repository lifecycle work below. This corpus is not a Hub implementation or live
acceptance result.

## Objective and authority

Continue development until native Claude Code meets the Hub capabilities in the
[accepted parity matrix](CLAUDE_LEAD_REVIEW_PLAN.ru.md#что-означает-сопоставимая-поддержка-с-codex),
and eliminate silent queue, approval and execution waits. The text-only Claude
scaffold does not meet that objective. Track remaining maintenance and live debt
in the [stabilization plan](STABILIZATION_PLAN.md); do not repeat completed stages.

The owner authorized repository implementation, offline tests and delegated
work. Live-state changes, credentials, service restarts, deployment, tags, branch
deletion and merging into `main` are not authorized by this task. Prepare concrete
checks/artifacts before requesting separately scoped live actions. Private handoff
claims are pointers to recheck, never facts to copy into Git.

The owner accepted initial no-progress thresholds of **300 seconds** for an
ordinary active turn and **1,200 seconds** for an active tool/build. Both must be
configurable. They trigger notices only; they never approve, stop, unlock or replay.

## Start safely

1. Follow the complete read order in [AGENTS.md](../../AGENTS.md), including
   the private profile and handoff. Read all normative modules before contract or
   trust-boundary changes, then the [Claude plan](CLAUDE_LEAD_REVIEW_PLAN.ru.md),
   [status](../status/PROJECT_STATUS.md), relevant ADRs and owning code/tests.
2. Inspect HEAD, dirty files, worktrees, hooks, dependencies, hotspots and open
   work. Preserve other agents' checkouts and the pending update-tool branch.
   Locate the continuation worktree through the private handoff; verify its
   existence, branch and diff. Never switch a shared checkout. Long-lived
   worktrees belong outside temporary directories and record their owner,
   purpose, base revision and post-merge review point privately.
3. Resume the owner's goal explicitly in the new session. Do not mark it complete
   after a plan, a prototype or offline tests alone. Report unsupported capabilities
   honestly; ask only decisions that change the result. Roles, subscription mode
   and scoring scope are already recorded.
4. Complete package A against current official Claude documentation and passive
   installed-CLI version/capabilities. Verify human approval hosting, exact resume,
   stop, local transfer, saved-session discovery and enforceable advisor isolation.
   Separate documented capability, adapter tests and authorized live evidence.
5. Reconcile deferred capacity and canary-root questions with bounded read-only
   inspection if needed. Use a schema-compatible read-only path; never initialize
   or migrate production state from a newer development checkout. Report necessary
   values privately; do not change capacity, bindings or another project's work.

## Development team

Primary: `gpt-6-astra`, effort `high`. It owns architecture, normative contracts,
security decisions, integration, exact-revision evidence and owner communication.
Helpers: at most `gpt-6-sol`, effort `high`, with standard service and no priority, under the current owner instruction.
Supply self-contained bounded tasks and required sources. Use an interface that
can enforce the service tier; the CLI supports explicit `service_tier=default`
and disabled fast mode. Verify model availability; never silently substitute. Official reference:
[Codex subagents](https://developers.openai.com/codex/subagents).

Use at most three concurrent helpers with four total session slots. The owner's
Sol/service-tier restriction takes precedence over the ordinary Gemini-helper preference here.
Each helper gets an owner, allowed files, base revision, tests and exit criteria.
Never delegate private profiles, credentials or deployment inventories.

| Helper lane | Initial read-only task | Implementation after lead design |
| --- | --- | --- |
| Claude runtime | Official capability matrix; adapter/session/model seams; failure taxonomy | Native stream adapter, models/effort, exact identities, partial/result checkpoints, provider fixtures |
| Task visibility | Admission, FIFO/root capacity, approvals, progress, retry, stop, sender ownership | Durable transitions/notices and passive no-progress detection behind the agreed state API |
| Acceptance | Existing fault tests, migrations, replay/stop certainty, regressions | Adversarial fixtures, restart/ambiguity tests and integration checks |

Start all three with investigation only. The lead defines state API, transaction
ownership and invocation boundaries before writing starts. Each writing helper
uses a distinct worktree/lane and disjoint modules, never the primary checkout.
Schema, migrations, worker wiring and normative documents have one named author
at a time. Integrate sequentially and test the integrated revision. No recursive
delegation or additional writer authority. Sol checks supplement, but do not
replace, independent review of Codex-authored lifecycle/security changes. The
owner explicitly selected **Claude Opus 5.5, effort high** as the reviewer. If
that reviewer becomes unavailable, use **Gemini** as the owner-authorized
fallback. Determine availability from supported passive metadata or an actual
review failure, never synthetic inference/quota probes. Do not silently substitute
another Opus version; resolve the exact supported model ID before invoking it.
Record the actual reviewer/model, fallback reason, candidate revision, findings
and fixes in the PR. An unavailable reviewer does not waive review. If both
review paths fail, keep the candidate review-ready and ask the owner for review;
never invent approval. Review is read-only in a separate clean candidate checkout,
and does not grant permission to run unrelated live tests or deployment.

## Ordered packages

### 1. Contracts and failing behavioral tests

Define visibility in the owning [persistence module](../product/PERSISTENCE_AND_RECOVERY.md),
approval boundaries in [control/security](../product/ACCOUNTS_CONTROL_AND_SECURITY.md),
and any changed [interaction](../product/IDENTITY_AND_INTERACTION.md). Update only
reviewed affected section digests and record consequential durable choices in an
ADR. This plan is not another normative contract.

Write failure tests before router changes. Document hotspot architecture review
and extraction decisions. Preserve one owner per SQLite transaction and import
direction. Reuse Controller, workers, SQLite queue/outbox and admission seams;
no separate broker, database or general orchestrator.

### 2. Visible and safe task lifecycle

Implement all six owner requirements through durable state:

- Approval immediately prepares a source-topic notice naming what stopped, the
  required permission and how the human allows, denies or stops. Hub/Hermes
  cannot approve; retain Codex/tlive or an accepted native human host. Unsupported
  approval modes explicitly deny instead of waiting forever.
- Meaningful provider events are separate from worker heartbeat and Telegram
  typing. Active tools/builds use the longer configured threshold. Notify once
  on transition; recovery re-arms the detector. No monitor inference.
- Distinguish accepted, queued, executing, approval-waiting and unknown outcome.
  Explain slot/global capacity/FIFO/root blockers, identify the owner topic by
  numeric identity and provide next actions. Acceptance is not invocation.
- A retry attached to an active turn says it joins existing work and neither
  restarts it nor removes the root lock. Distinguish the existing reply-bound
  inspection-first continuation after proven terminal failure. Unsupported
  retries fail visibly.
- `/stop` states numeric-topic scope, queued cancellations, pending/confirmed
  interruption, held work and remaining tasks in other topics. A stop request
  or successful interrupt RPC is not terminality proof.
- Persist transitions, episode identity, notices, retry deadlines and receipts.
  Never duplicate execution. Bot API has no general exactly-once send key:
  ambiguous acceptance must be handled explicitly, without blind resend or a
  false guarantee that database deduplication prevents duplicate messages.

A timeout never releases the root. Confirm the exact turn terminal before
release; unknown stop/outcome retains exclusion and explains reconciliation.
Never automatically repeat old user work. Cover applicable compatibility paths
or reject unsupported configurations visibly.

The schema-37 slice now has queue admission/handoff snapshots and accepted Codex
activity wired to the existing sender; see
[ADR 0051](../decisions/0051-accepted-turn-activity-and-queue-notices.md). Do not
restart that implementation as a new task. Its exact-revision publication
validation and independent reviews are complete. The
[notice-bound active-work retry candidate](../decisions/0054-notice-bound-work-retry-reports.md)
has offline implementation; complete its publication/review gates separately.
Address remaining retry surfaces,
approval before native turn acceptance and explicitly scoped visibility for
other execution paths next.

### 3. Claude runtime and human authority

Complete packages B/D of the accepted Claude plan: native identity, supported
passive model/effort discovery/configuration, exact start/resume and root binding,
structured visible streaming, inputs/artifacts, progress, human approvals, stop,
durable recovery, passive telemetry, same-session `/local`/`/return`, and saved
session connection. Record every capability's evidence in the existing matrix.

CLI documents `--permission-prompt-tool` and `--permission-prompts host|none`;
these are candidates, not a validated Hub approval host. Investigate tlive's
Claude `PermissionRequest` support and writer isolation before enabling tools.
Codex's approval-only marker does not establish the Claude equivalent. Restrict
tools/MCP/hooks/plugins/skills/children technically; missing host, restart,
timeout and ambiguity deny safely.

Keep CPA route preflight; separately prove the subscription/no-paid-fallback
route. Never extract OAuth credentials or infer SDK subscription authorization
from CLI login. Quota/provider terminal rejection and unknown outcome receive
different classifications; neither permits automatic replay. Persist native
identity before invocation wherever supported.

### 4. Bounded collaboration and supporting maintenance

Complete packages C/E: either provider can lead, advisor is technically read-only,
role transfer requires a proven safe boundary, one bounded review and continuation
are durable/idempotent, and minimal outcome/usage records preserve unknowns.
The lead releases its FIFO slot before the advisor runs. Keep scoring, judges,
parallel writers and recursive/write-capable helpers deferred.

Address directly supporting debt: Claude failure classification, duplicated slot
identity and lifecycle/dispatcher seams. Continue remaining stabilization and
update-plane items in their existing plans after checking ownership/priority;
do not mix the pending update-tool PR or host migration into this feature branch.

### 5. Verification, independent review and separate live acceptance

Offline scenarios: unhandled/unreachable approval, hung tool, long productive
build, retry active work, another-topic queue/root owner, `/stop`, restart while
blocked, unknown interruption, role/session/root mismatch, partial/completed
Claude output, quota rejection, delivery ambiguity, stale callbacks, duplicate
inputs and zero inference from passive monitoring. Use injected clocks and fake
structured providers. Test both lead/advisor orders, Codex/other-provider
regressions and additive migration backup/rollback.

Follow the [testing guide](../testing/README.md): focused iteration, history/privacy
before every commit, pre-commit hook, canonical gate on the publication revision,
required hosted checks and independent exact-revision review. Never bypass hooks
or publish a red suite. Open focused PRs; merging remains the owner's action.
Offline tests do not establish deployed Telegram behavior or a subscription route.

Prepare bounded live scenarios with exact candidate/rollback revisions, commands,
temporary root/topic, provider calls, expected effects and cleanup before asking
permission. Request provider acceptance, any restart and deployment separately.
Update private handoff whenever plan, pending decisions or deployment state changes.

## Exact Telegram control provenance and continuity

Source owner: Hub maintainer; sole writer. Schema50 source at clean
`4727241b3906c27ab4556cd4abd8eb6157e0c4f6`, lane
`feat/codex-telegram-provenance`, passed canonical 2,654 tests in 239 modules,
Pyright zero, privacy/history, independent Astra and actual Claude Opus 5.5/high
reviews and all seven exact-head hosted checks. It retains fresh immutable
admission/acceptance provenance and first-input/control fences; no automatic
precaution is enabled. Owner merge and live acceptance remain separate.

The next persistence-only slice is schema51 in lane `feat/codex-ingress-episodes`,
based on that revision. Root owns implementation; Astra reviewed causal ordering
and DDL boundaries. Producer continuity and exact-target assessments are under
offline validation; publication and independent exact-source review remain open.
Next trigger: complete those gates, then design atomic full-control consent and
exact commentary egress inside the existing shared send fence. The three-cycle
budget, root uncertainty and no-replay policy remain unchanged. Closure is open
until live/maintenance integration and separately authorized channel-loss
acceptance pass; neither restart nor this storage prerequisite is acceptance.
Inspect tracked, staged and untracked lane state before any post-merge cleanup.

## Current evidence and source pointers

The schema-36 stop/control-delivery slice passed canonical checks and independent
review at the revision above; see
[ADR 0049](../decisions/0049-task-visibility-and-stop-certainty.md). The native Claude slice adds identity preparation, bounded streaming and
saved-result recovery; canonical and hosted gates passed at `c5e130c`, while
exact-candidate independent review and merged-revision validation are complete.
See [ADR 0050](../decisions/0050-claude-native-invocation-evidence.md). Queue snapshots and accepted Codex activity are present in the schema-37
baseline. Its publication gates and independent review are complete; active retry,
approval before acceptance, deployment acceptance and full Claude parity remain open.
No deployment or live acceptance follows from these repository checks.

First-pass pointers to recheck, not final design:

- `external_runtime.py` / `claude_stream.py`: bounded text-only native stream;
  `catalog_refresh.py`: Claude catalog is just the configured default;
  `local_transfer.py`: no Claude resume branch.
- `codex_appserver.py`: shared approvals wait for the companion; fallback declines.
  Add source-topic visibility while preserving human approval ownership.
- `external_worker.py`: the reproduced uncertainty-release gap is addressed by
  the stop-certainty slice; exact terminal proof remains mandatory.
- `state_provider_jobs.py`, `root_blockers.py`, `execution_journal.py`,
  `outbox_sender.py`, `controller_admission.py`: root/FIFO/lease/notice seams.

## Closure

Open. Close only when parity and task-visibility contracts have exact-revision
evidence, independent review and separately authorized required live acceptance.
Unsupported required capabilities remain owner decisions, not silently removed
criteria. Update the accepted Claude matrix and next trigger as evidence changes.
