# Stabilization source candidates

Status: historical record, moved unchanged from the
[stabilization plan](STABILIZATION_PLAN.md) on 2026-11-07 to keep the plan
readable. Each section keeps its own status line; open live acceptance stays
tracked in the plan's [live-acceptance backlog](STABILIZATION_PLAN.md#live-acceptance-backlog).

## Current source integration checkpoint

The former stop/tlive integration and minimal developer rollout are closed
under the [current checkpoint](NEXT_DEVELOPMENT_SESSION.md#current-goal-and-order).
Do not replay historical installation executors or reopen ancient sessions.
The temporary-Bash increment has separate publication and live gates.
Broader control-loss, outcome-journal, three-project, Hermes, Hub application
recovery and release milestones remain open; full-host infrastructure is external.

## Incident candidates

Journal: [checkpoint](INCIDENT_JOURNAL.md#source-checkpoint).
Hermes notices: [checkpoint](HERMES_INCIDENT_NOTICES.md); live/Analyze open.

The [tlive subscription fix](../../integrations/tlive/README.md) has offline
stalled-history, card and disconnect regressions. The
[Claude parent UID policy](CLAUDE_FILE_PERMISSIONS.md) restores runtime validation.
Source reviewed; [stop follow-up](NEXT_DEVELOPMENT_SESSION.md#owned-claude-stop-certainty-follow-up) and live gates remain open.
Combine Claude editor acceptance with Hub queue, delivery, stop and exact-session
recovery checks, preserving uncertain work and genuine human Write/Edit decisions.

## Read-only outcome projection candidate

The bounded projection and schema-45 owner assessments are implemented in source;
see the [journal runbook](OUTCOME_JOURNAL.md) and
[REQ-EVAL-010](../product/EVALUATION_AND_ALLOCATION.md). Projection foundation
`dcc650794719a1be6c4143766bdb007265352471` passed its canonical and seven hosted
gates after independent review; that historical result is not live acceptance.
Requested/stored/observed provenance remains distinct; missing native time/usage
stays unknown. P6 closes remaining provenance and the real assessment/correction
canary rather than reimplementing this journal. Roles remain retired.

## Advisor foundations integration candidate

Published source: `feat/advisor-foundations-integration`, base `dcc6507`, with
namespace/capsule histories `fcc755f` and `cd8b604`. Owner: Hub maintainer, sole
writer. Clean `1c70d5639b59cfa97829bb56deaf3f95f577cb42` passed commit/canonical
gates (2,149 tests/195 modules, types/static/privacy/history), exact-head Astra
and actual Opus review, and seven hosted checks including strict namespaces.
Affected corpus: 123 tests, no namespace skips; Python 3.11 lacked the memfd
wrapper, and a later Python 3.12 subset passed 33 tests.

Shared private-source and case-sensitive lookup guards apply to every profile.
ADRs 0056/0057 own the descriptor, alias and caller-authorization boundaries;
scripted lookup/FD fixtures are not real casefold/XFS or installed custody proof.
Advisor material/workflow and inference transport follow-ups are withdrawn by
ADR 0065. Retain shared Claude/security fixes on independent merits. Private
networking still blocks host loopback; integration and deployment remain separate.
Inspect all lane state before any post-merge cleanup; cancellation is not
productive acceptance.

## Offline native Claude transport follow-up

Current state: published source candidate `535c0b8` in lane
`test/claude-native-transport-corpus`, based on clean `1c70d56`; canonical
publication passed with 2,175 tests and Pyright 0. Exact-candidate Claude Opus
review has no remaining mandatory findings; Astra reviewed the production
follow-up. All seven hosted checks passed at exact head `535c0b8`, including the
Python 3.11/3.12/3.13 matrix and namespaces. Source owner: Hub maintainer.
The optional corpus uses an explicitly supplied native CLI with
synthetic credentials and an HTTP fixture inside private network/PID/IPC
namespaces; host files and loopback have positive and negative controls.

The four bearer/API-key × SSE-success/HTTP-529 cases exposed two compatibility
fixes: safe mode leaves optional built-in mods enabled, so both settings builders
now explicitly disable the known optional IDs; native API rejection uses the
success result variant with an error flag and explicit HTTP status. The strict
plugin guard remains unchanged, required security policy mods remain untouched,
and incomplete/contradictory HTTP-error evidence remains indeterminate. Verified
rejection does not establish an absence of earlier effects or authorize replay.
The process fixture additionally kills its owned group before reaping an exited
leader, and refuses special-file executable sources before open. The final
corpus inherits production start argv and checks its exact fictional session
identity. Strict native execution pins local binary/version evidence and requires
the exact 529 envelope, zero visible assistant messages on rejection and fully
validated host diagnostics. All four cases passed with CLI 2.1.285; additional
retry/output bounds belong only to the fixture.

The advisor-specific isolated transport follow-up is withdrawn by ADR 0065.
Retain the standalone native/error corpus; hosted confirmation at exact `535c0b8`
is recorded above, so no unchanged gate is repeated. The
[testing guide](../testing/README.md#optional-offline-native-claude-transport)
owns the procedure and evidence limits. This does not close subscription routing,
CPA/no-paid-fallback, human file approvals, local transfer or Telegram acceptance.

## Bounded review pipe primitives

Retained offline source/fixtures under
[ADR 0058](../decisions/0058-bounded-review-pipe-primitives.md), owner Hub maintainer:

| Source | Scope and completed source gates |
| --- | --- |
| `dd43ae6`, `feat/advisor-inference-bridge`, base `535c0b8` | Framing/one-use fake gate; canonical 2,204 tests/198 modules, types/privacy, Astra/actual Opus and seven hosted checks |
| `e563dd4ca51b5d4884227681782d4a7fce275b2e`, `feat/advisor-pipe-sequencing` | Directional ordering/finite partial writes; canonical 2,241 tests/200 modules and the same independent/hosted gates |
| `cc976f6828554104b7326770f1e3a67b6e2de6ef`, PR #154 | Owned actual pipes/private namespace witness; canonical 2,264 tests/203 modules and independent/hosted gates |
| `cad8e8d8732bfb52aadb45e15d783785af4da84a`, PR #174 | Native corpus + PR #154 foundations joined; source gates passed |
| `496a719707a5b02557e659e77facf6a481f2a4da`, PR #175 | Test-only exact HTTP envelope/semantic owner; source gates passed |
| `851714b5005553906b29c771fcd095de133f776b`, PR #176 | Owned raw exchange extraction; canonical 2,855 tests/256 modules, types/privacy, Astra/actual Opus and seven hosted checks |

Fixtures track owned allocations; the earlier FD-count discrepancy is unattributed.
The [testing guide](../testing/README.md#offline-review-pipe-primitives) owns
commands and limits; offline fixtures establish no live acceptance.
ADR 0065 withdraws advisor integration. Retain tests/cleanup, do not repeat gates;
reviewed source integration, separately authorized deployment and post-merge
worktree inspection remain required.

## Claude process observations

Current state: source published in PR #155 at clean
`a542b2054250dcd7e60ad7df0969ca7462e4054b`, based on prerequisite
`e563dd4ca51b5d4884227681782d4a7fce275b2e`. Source owner: Hub maintainer.
[ADR 0059](../decisions/0059-claude-process-observation-notices.md) records the
separate process/permission evidence boundary. Focused actual-process, SQLite
and migration fixtures passed. Canonical publication passed 2,277 tests in
204 modules, full Pyright and privacy/history; exact clean GPT Astra and actual
Claude Opus 5.5/high reviews found no remaining blockers. All seven hosted checks
passed at that exact head. These are source/offline and hosted checks.

Next trigger: owner integration and separately authorized native
and Telegram acceptance alongside the remaining Claude parity work. This lane
is bounded to optional passive ordinary quiet notices; tool/build activity,
local transfer, saved-session connection and billing
route acceptance remain open. Root is the sole implementation writer. The
worktree owner is Hub maintainer; its purpose is schema-42 observation and
worker/sender integration, with base `feat/advisor-pipe-sequencing`. Post-merge,
inspect tracked, staged and untracked state before worktree cleanup. Closure is
open; no deployed or live acceptance is claimed.

## Final/progress delivery certainty prerequisite

Status: schema43 source implementation under validation. Base verified revision:
`a542b2054250dcd7e60ad7df0969ca7462e4054b`; exact candidate publication remains
pending. Root is the sole writer; Hub maintainer owns this worktree. Its purpose
is durable send fencing, shared final-part policy, provenance and preservation
of late native proof. Base branch: `feat/claude-process-observations`. After owner
integration, inspect tracked/staged/untracked work before any cleanup.

The [owning contract](../product/PERSISTENCE_AND_RECOVERY.md) and
[ADR 0060](../decisions/0060-final-and-progress-delivery-certainty.md) define the
change. Next trigger: exact-candidate source reviews, canonical/privacy and
hosted gates, then the bounded owner-assessment slice. Deployment, Telegram
acceptance and unknown-delivery reconciliation remain separate; no provider
replay, writer change, service change or assessment command is enabled here.
Closure: open until exact source publication gates pass; live backlog stays open.

Schema43 activation gate: unknown final/notice delivery currently blocks later
topic deliveries, and result-ready heads block new productive work. Before any
activation, independently review the schema44 local-owner reconciliation
action that retains parts/results/spool, never fabricates delivery or replays
provider execution, and safely resolves the topic hold. Source publication of
this prerequisite does not waive that gate. Review also requires populated-copy
foreign-key/orphan-part preflight before the migration. Source owner: Hub
maintainer; decision owner: repository owner; last verified source base:
`9401becf230d74b516bec902bae44c6ddbaec78c`. The schema44 action is implemented
in source under validation; [ADR 0061](../decisions/0061-owner-delivery-hold-dispositions.md)
records its ownership and limits. Next trigger: exact-candidate review and
publication, then separately authorized live rollout. The broader live backlog
remains open.

Delivery-hold follow-up: schema44 queue continuation retains indefinite
`result_ready` session-control, scope-wide local transfer and agent-drain blockers,
plus lifetime topic binding. Source publication is complete at
`aac70181d21a00a51104ebbfe459f2e987fc4128` with exact-revision independent
reviews and canonical/hosted gates. Hub maintainer owns the next separately reviewed full-control
reconciliation decision before claiming complete operational acceptance. No
timeout, restart, automatic resend or provider replay may clear these boundaries.
See [ADR 0061](../decisions/0061-owner-delivery-hold-dispositions.md).

## Owner outcome assessments

Status: source publication complete in PR #158 at
`bc7a2ab29fe0ad699eb39ee22b85a7143a5f3c16`; source owner: Hub maintainer,
root is the sole writer. Worktree purpose: bounded human outcome journal,
base branch `feat/delivery-hold-reconciliation`. Design and transaction/batch
ownership were independently reviewed by GPT Astra.
[ADR 0062](../decisions/0062-owner-outcome-assessments.md) records the decision;
the [runbook](OUTCOME_JOURNAL.md#owner-decision-procedure) owns the procedure.
Closure: malformed-input and migration/consumer/recovery follow-ups passed
focused checks; exact GPT Astra and actual Claude Opus 5.5/high reviews have no
remaining mandatory findings. Mandatory pre-commit and clean-commit canonical
pre-push passed 2,372 tests in 215 modules, Pyright 0 and privacy/history; all
seven exact-head hosted checks passed. PR #158 is ready and unmerged.
Next trigger: owner integration, then independently authorized custody and
Telegram acceptance. Scope does not include productive
collaboration, usage provenance, scoring or judges. Deployment, journal custody
and Telegram acceptance remain independent gates. After owner integration,
inspect all tracked/staged/untracked worktree state before cleanup. Closure is
complete for source publication; the broader goal remains open.

## Canonical lane archive correction

Status: source publication complete in PR #159 at
`07fb1cfe088e0ac9e013acb7c46e60aab52ab685`; seven exact-head hosted checks passed.
Owner: Hub maintainer, sole writer.
Worktree purpose: repair canonical-root persistence after archive/restart;
base branch `feat/owner-outcome-assessments`. GPT Astra reviewed the minimal
root-validation and transaction design. The old-code regression reproduced
historical-root normalization after CLI archive. The
[dated ADR correction](../decisions/0031-bounded-root-concurrency.md#archive-correction-2026-10-08)
records the restored boundary. Legacy evidence protections, schema-44 consent
limits and cleanup remain unchanged. Next trigger: owner integration and separately designed full-control
reconciliation. After owner integration inspect tracked, staged and untracked
work before cleanup. Source publication closure is complete; no deployment
or earlier-topic repair is authorized here.
Exact GPT Astra and actual Claude Opus 5.5/high reviews have no remaining
mandatory findings after correcting the over-broad idle-peer guard. Commit and
canonical publication checks passed 2,401 tests in 216 modules, with no typing
errors. These checks do not establish deployment or live acceptance.

## Lane-state domain extraction

Status: neutral source publication complete in PR #160; source owner: Hub maintainer,
root sole writer. Base revision:
`07fb1cfe088e0ac9e013acb7c46e60aab52ab685` in `fix/canonical-lane-archive`.
Worktree purpose: move cohesive lane queries, binding/archive policy and cleanup
SQL to a dependency-neutral domain before full-control delivery reconciliation.
GPT Astra reviewed the extraction boundary. HubState retains public signatures,
one connection and all immediate/plain-write transaction ownership; registry/Git
validation, topic observation, legacy normalization and session policy remain
with their existing owners. No schema or observable control exception is added.
New characterization checks cover register/bind rollback and real cleanup commit
contention; existing archive/history/peer/source restrictions remain required.
Source publication complete at `61657f24592c43c5e57cd551d9484fd672bc5512`:
commit/canonical 2,405 tests in 217 modules, Pyright 0, privacy/history and all
seven exact-head hosted checks passed. GPT Astra and actual Claude Opus 5.5/high
source reviews have no mandatory findings. PR #160 is open and unmerged.
Next: the separately reviewed full-control contract and ledger. After owner
integration, inspect tracked, staged
and untracked state before any cleanup. No deployment or restart is authorized.

## Delivery control storage and preview prerequisite

Status: schema46 source published at `357409b0d52a6796a3fe609f9f49c2b3bb918817`;
PR #161 ready/open/unmerged, full-control authority unavailable in that revision.
Owner: Hub maintainer, root sole writer. Branch `feat/delivery-control-preview`
owns additive ledger/read-only preview; base `61657f24592c43c5e57cd551d9484fd672bc5512`.
Exact Astra and actual Opus 5.5/high source reviews have no mandatory findings.
Commit/canonical: 2,422 tests/219 modules, Pyright 0, privacy/history and seven
hosted checks passed. [ADR 0063](../decisions/0063-delivery-control-reconciliation.md)
owns the boundary. Next trigger: coordinated authority below. Closure remains open
until owner integration; inspect tracked/staged/untracked state before cleanup.
Source publication grants no migration, deployment, restart or live acceptance.

## Coordinated delivery control authority

Status: schema47 implementation under validation. Owner: Hub maintainer, root sole
writer. Branch `feat/delivery-control-authority` owns coordinated public CAS and
consumers; base/last verified source `357409b0d52a6796a3fe609f9f49c2b3bb918817`.
GPT Astra reviewed design, dirty-source consumer and retention inventory. Three
findings were repaired: coherent status, separate legacy/full effect and final
job/result fail-closed parity. [ADR 0063](../decisions/0063-delivery-control-reconciliation.md)
and [runbook](DELIVERY_CONTROL.md) own design/procedure. Next trigger: complete
consumer/race/retention/migration tests, canonical gates, exact independent review
and hosted publication. Closure open; owner integration and private live gates
remain separate. Inspect tracked/staged/untracked state before post-merge cleanup.

## Planned final-response mode indicators

Owner-requested follow-up (2026-10-07): goal-only offline candidate; broader
mode visibility remains open. The owning
[REQ-CMD-001](../product/ACCOUNTS_CONTROL_AND_SECURITY.md#compact-control-surface)
defines exact-turn observations and recovery limits. Codex CLI 0.159.2 generated
schemas establish event shapes only. `/fast` remains unknown: service-tier
settings may affect subsequent turns and establish no current-turn mapping.
No provider RPC or schema is added. Owner: Hub maintainer. Last verified base:
`008a1a95c78ee77893448b700ef866940c8baae1`. Next: exact-candidate source review
and publication, supported fast evidence, then separately authorized Telegram
acceptance. Other consequential modes remain follow-ups; closure is partial.
