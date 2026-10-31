# Evaluation and resource-aware task allocation requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#evaluation-and-allocation).

Status: accepted direction; implementation and live acceptance pending.
The minimal private journal is independent of retired Hub roles. Automatic scoring,
ranking, allocation, model judges and trials remain deferred until need and evidence
justify them; this baseline enables no calls, providers or deployment and adds no
current provider-parity gate.

## 21. Evaluation and resource-aware task allocation

- **REQ-EVAL-001 (Accepted):** Hub MUST support evidence-based outcome/profile evaluation for
  recommendations and explicitly enabled allocation. Profiles MUST distinguish
  task/role/runtime/model/effort/configuration/context, separate suitability from resources, and
  never allocate from one global score alone.
- **REQ-EVAL-002 (Accepted):** Evaluation MUST distinguish completion, first-pass acceptance,
  defects, rework, decision quality, execution/wait time and observable total cost, including
  descendants without double counting. Provider success MUST NOT imply product acceptance; unknown
  attribution, cancellation and immature outcomes MUST NOT become invented grades.
  - Provider, transport, environment and integration failures MUST remain separate from participant
    mistakes, with attributable defect severity retained.
- **REQ-EVAL-003 (Accepted):** Estimates MUST retain source, task stratum, sample support,
  uncertainty, freshness and relevant versions; missing evidence stays unknown. Comparisons MUST
  account for difficulty, selection and conditions, calibrate probability claims, and review
  applicability after material changes.
- **REQ-EVAL-004 (Accepted):** Objective checks and model judgments MUST stay separate; evaluators
  MUST NOT be sole correctness, approval or accounting authorities. Their calibration, bias,
  failures and cost require held-out validation, and a useful deterministic fallback MUST remain
  without a required vendor.
- **REQ-EVAL-005 (Accepted):** Allocation MUST use allowlisted accounts and passive or
  already-authorized observations, never inference/exhaustion probes.
  - Limits MUST retain scope, units, window/reset, time and source, distinguishing
    reported/estimated/stale/exhausted/unknown values.
  - Context, usage, quota, rate limits and money MUST remain distinct; shared pools MUST NOT be
    counted twice and unobservable capacity MUST stay conservatively reserved/unknown.
- **REQ-EVAL-006 (Accepted):** Allocation MUST first enforce owner pins, access, capability,
  sandbox, approvals, writer exclusion, capacity and budget; only delegated executor selection MAY
  be automatic.
  - Versioned ranking MUST explain decisions and allow safe owner override without overriding
    admission.
  - Reply/mention/session/enqueue targets MUST remain intact; reallocation requires a new explicit
    decision, never silent in-flight migration or uncertain replay.
- **REQ-EVAL-007 (Accepted):** Productive evaluation/judging/duplication MUST require explicit
  workflow authority and bounded budget, never originate from monitors, timers or recovery probes.
  All descendant cost and unknown usage MUST be conservatively bounded; exhaustion stops
  discretionary admission and MUST NOT be bypassed through paid fallback, purchases or rotation.
- **REQ-EVAL-008 (Accepted):** Trials MUST retain comparable inputs/artifacts/criteria/conditions
  and recorded assignment, isolate candidates and prevent duplicate external effects. Costs MUST
  include judging/discarded work without presumed cancellation refunds; limited trials cannot prove
  unrelated superiority, blinding SHOULD control bias, and exploration MUST justify its decision
  benefit.
  - Exploratory, observational and paired evidence MUST remain distinguishable; judgments SHOULD
    control candidate order as well as identity bias.
- **REQ-EVAL-009 (Accepted):** Evidence MUST be durable, versioned, idempotent and reproducible,
  with correction ownership and honest provenance loss after deletion.
  - Private bounded records MUST use restrictive permissions; shared evaluation/Git MUST exclude
    reasoning, credentials, raw environment/screens and deployment identities.
  - Cross-project/external evaluation MUST require explicit data scope; models MUST NOT edit their
    own authoritative grades, usage or allocation receipts, without claiming same-UID/admin tamper
    resistance.
- **REQ-EVAL-010 (Bounded projection and owner assessments implemented offline; live acceptance
  pending):** Hub MUST provide a private task/result/participant journal with owner
  accepted/rework/unknown decisions, reasons and observable time/usage; unknowns stay unknown and no
  scorer/judge/dispatcher is required.
  - Only configured owners at central registered-topic ingress with external queue/outbox MAY
    assess; reserved commands MUST be handled before routing or productive preparation.
  - Invalid, quoted/material-bearing or unavailable targets MUST receive durable refusal without
    productive fallback; forwards remain passive.
  - First assessment MUST Reply to one exact same-destination saved final with strict positive
    provenance for every part, never progress, controls, incomplete/unknown delivery or legacy
    receipts.
  - Historical completed generations MAY be assessed without retargeting; corrections MUST Reply to
    the latest applied human command, with at most one successor.
  - Applied/refused decisions MUST be append-only and deduplicated before parsing; conflicts
    preserve the prior disposition without invocation or endless retry.
  - Disposition, consumed input, independent acknowledgement and topic command boundary MUST commit
    together; failures stay control-only retryable and duplicates MUST NOT close later batches.
  - Assessment MUST preserve holds, routing, status, root exclusion and capacity; acknowledgement
    retains strict receipt/no-blind-resend rules.
  - Unsupported/scoped-actor endpoints MAY refuse once; noncentral pollers MUST NOT consume shared
    receipts and private controls MUST NOT enter other workflows.
  - Rework MUST NOT enqueue work, acceptance MUST NOT grant tools/release uncertainty, and unknown
    MUST NOT overwrite usage/model/time evidence.
  - Confined Claude MUST lack journal access; authentication/SQL MUST NOT claim deployed custody,
    global same-UID exclusion or acceptance from trusted administrative access.

### Acceptance boundaries

Journal acceptance MUST prove duplicate/conflict/correction handling, exact Reply
provenance, atomic rollback and no productive fallback. Unknowns MUST stay unknown;
assessment MUST NOT invoke, replay, grant tools or release execution uncertainty.

Broader acceptance MUST prove conservative evidence, resources, budgets and pins
under missing/stale data, corrections, outages and manipulated content, with zero
passive inference or unapproved exploration. Trials MUST NOT publish/integrate both
candidates as productive results.

Quality/savings claims MUST use representative repeated comparisons against fixed
routing, including overhead, uncertainty and failed/discarded work; inadequate
historical assignment cannot prove superiority. Deployment/provider acceptance
remains separately authorized for the exact clean revision.

The [research report](../research/AGENT_SCORING_AND_ALLOCATION.ru.md) and
[ADR 0037](../decisions/0037-evidence-based-task-allocation.md) retain methods and rationale;
specific formulas/vendors remain implementation choices.
