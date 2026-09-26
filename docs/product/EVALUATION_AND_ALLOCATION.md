# Evaluation and resource-aware task allocation requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

Status: accepted product direction; implementation and live acceptance pending.
The requirement is foundational: future task orchestration must preserve the
evidence needed to evaluate participants and allocate work. No scoring service,
automatic provider switch, evaluation run, new provider, or deployment is enabled
by this document. Algorithms and evaluator vendors remain implementation choices.
Rationale: [ADR 0037](../decisions/0037-evidence-based-task-allocation.md).

Delivery priority: the initial [lead/advisor milestone](../operations/CLAUDE_LEAD_REVIEW_PLAN.ru.md)
uses human-selected participants and a minimal outcome/usage journal. Automatic
profiles, ranking, allocation, model judges and comparative experiments remain
deferred until observed need and suitable evidence justify them. The accepted
long-term requirements below do not make those mechanisms a prerequisite for
the first collaboration release.

## 21. Evaluation and resource-aware task allocation

- **REQ-EVAL-001 (Accepted):** Hub MUST support automatic evidence-based
  evaluation of task outcomes and participant profiles, and use those profiles
  for recommendations and explicitly enabled task allocation. Profiles MUST
  distinguish task class and role, runtime, provider, model/version, effort,
  material tool/instruction configuration and context conditions. General
  suitability and present resource availability MUST remain separate. One
  global model score MUST NOT be the sole basis for allocation.
- **REQ-EVAL-002 (Accepted):** Evaluation MUST distinguish verified task
  completion, first-pass acceptance, attributable defects and severity, rework,
  decision quality, execution time, queue/approval/wait time, and observable
  resource consumption. Full task cost MUST include delegation, evaluation,
  retries and integration without counting child consumption twice. Provider,
  transport, environment and integration failures MUST be distinguished from
  participant mistakes; unknown attribution, cancelled work and immature
  delayed outcomes MUST NOT become fabricated success or failure labels.
  A provider's terminal success alone MUST NOT establish product acceptance.
- **REQ-EVAL-003 (Accepted):** Every displayed or consumed profile estimate
  MUST retain its evidence source, task stratum, sample support, uncertainty,
  freshness and relevant model/evaluator/rubric versions. Missing evidence
  MUST remain unknown. Comparisons MUST account for task difficulty, selection
  effects and differing execution conditions. Model confidence MUST NOT be
  presented as empirically established success probability without calibration
  against outcomes. Material version or workflow changes MUST trigger review
  of profile applicability rather than silently pooling incompatible evidence.
- **REQ-EVAL-004 (Accepted):** Objective checks and model assessments MUST be
  recorded separately. An optional model evaluator MAY supply bounded
  classifications, rubric assessments or recommendations, but MUST NOT be the
  sole correctness oracle, approval authority or resource accountant. Its
  calibration, bias, failure behavior and full cost MUST be evaluated on
  representative held-out tasks. The system MUST retain a useful deterministic
  fallback when the evaluator is absent, unavailable or insufficiently proven.
  No particular evaluator, including Jev, is a required dependency.
- **REQ-EVAL-005 (Accepted):** Resource-aware allocation MUST use only
  allowlisted accounts and provider-supported passive telemetry, cached data
  or observations from already authorized work. Each limit MUST retain its
  scope, units, reported window/reset semantics, observation time and source.
  Reported, estimated, stale, exhausted and unknown states MUST remain
  distinguishable. Context occupancy, token usage, subscription quota, rate
  limits and money MUST NOT be treated as interchangeable. Shared quota pools
  MUST NOT be counted once per model or participant. Reservations and unknown
  consumption MUST be handled conservatively; exact remaining capacity MUST
  NOT be claimed when it is not observable. Monitoring and quota discovery
  MUST NOT invoke inference or synthesize an exhaustion probe.
- **REQ-EVAL-006 (Accepted):** Allocation MUST first enforce owner intent,
  recipient/model pins, project/data access, capabilities, sandbox, approvals,
  writer exclusion, capacity and budget. Within the eligible set, a versioned
  policy MAY rank estimated quality, time, total cost and resource scarcity.
  Hub MUST record why a candidate was selected, rejected or deferred, and
  allow owner override within the same safety boundaries. Learned scores and
  model recommendations MUST NOT override deterministic admission. Existing
  Reply/mention routing, provider-session identity and immutable enqueued
  target snapshots MUST remain intact; a changed allocation creates a new
  explicit task decision and MUST NOT replay uncertain work or silently
  migrate an in-flight turn. Automatic allocation applies only to work whose
  executor selection the owner has delegated, not to all ordinary chat input.
- **REQ-EVAL-007 (Accepted):** Productive evaluation, model judging and
  exploratory duplication MUST require an explicit task/workflow authorization
  and bounded budget. They MAY execute automatically within that authorized
  workflow, but MUST NOT originate from health checks, monitoring, recovery
  probes or scheduled timers. Accounting MUST include all evaluator and
  descendant calls; passive statistics and policy scoring MAY run without
  inference. Unknown usage MUST remain bounded by conservative reservations
  or call/time limits. Evaluation-budget exhaustion MUST stop admission of
  new discretionary evaluation work; paid fallback, purchases or account
  rotation MUST NOT bypass that budget. Existing explicitly authorized account
  rotation remains subject to the same workflow budget and account policy.
- **REQ-EVAL-008 (Accepted):** Comparative trials MUST use recorded, comparable
  task inputs, base artifacts, criteria and execution conditions, isolate
  candidates, and prevent duplicate external side effects. Trial selection and
  assignment MUST be recorded so that exploratory, observational and paired
  evidence cannot be mistaken for one another. Small-task results MUST NOT
  establish superiority for unrelated complex work. Judgments SHOULD blind
  candidate identity where practical and control order effects. Trial budgets
  MUST include judging and discarded candidates, and cancellation MUST NOT be
  assumed to refund consumption. Exploration policy MUST be justified by
  measured decision benefit rather than unbounded ranking activity.
- **REQ-EVAL-009 (Accepted):** Task, attempt, assessment, resource and allocation
  evidence MUST be durable, versioned and idempotent, with explicit ownership
  of corrections and delayed outcomes. Evidence-derived aggregates MUST be
  reproducible from retained eligible records; retention/deletion MUST mark
  reduced provenance rather than invent it. These records and participant
  profiles are private local state with bounded retention and restrictive
  permissions. Hidden reasoning, credentials, raw terminal/environment dumps
  and deployment identities MUST NOT enter shared evaluations or the repository.
  Cross-project aggregation and transfer to an external evaluator require
  explicit data scope; ordinary context isolation remains in force. Models
  MUST NOT edit their own authoritative grades, usage or allocation receipts.

### Acceptance boundaries

Implementation acceptance requires offline evidence for duplicate outcome
delivery, delayed corrections, attribution uncertainty, shared quota pools,
stale/missing telemetry, budget reservations, pinned recipients, cold-start
profiles, evaluator outage, manipulated candidate content and replay refusal.
The test suite must demonstrate zero inference from passive scoring/monitoring
and no unapproved exploratory work. Paired trials must not publish or integrate
both candidates as if both were productive user outcomes.

Quality or savings claims additionally require representative repeated
comparisons against a fixed routing baseline, including evaluation overhead,
quality constraints, uncertainty and failed/discarded work. Historical data
without suitable assignment coverage cannot prove counterfactual superiority.
Deployment/provider acceptance remains separately authorized and tied to the
exact clean revision under the existing engineering evidence rules.

The [research report](../research/AGENT_SCORING_AND_ALLOCATION.ru.md) explains
candidate metrics, statistical methods and Jev suitability. It is non-normative;
specific formulas, thresholds and experiments are not accepted implementations.
