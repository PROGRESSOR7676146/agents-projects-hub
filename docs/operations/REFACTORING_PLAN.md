# Refactoring plan

Status: Packages 0–5 complete; Package 6 deferred
Date: 2026-09-21

## Objective

Reduce the cost and blast radius of routine maintenance without weakening the
privacy/history gate, queue and transaction invariants, exact-revision evidence,
or the separation between repository checks and deployment-local acceptance.
Line count is not a success metric. Each extraction must make one behavior
easier to change and verify in isolation.

The work is deliberately split into independently reviewable packages. Finish,
publish, and merge one package before starting the next. A package that uncovers
an observable behavior, trust-boundary, schema, or deployment-contract change
stops being a mechanical refactor and must follow the corresponding normative
change process.

## Closure checkpoint

Packages 0–5 are published in `origin/main` at
`a00ab09994d552e83fba470f745539cb88052e63`. Package 0 was merged through PR
#56, Package 1 through PR #57, Package 2 through PR #58, Package 3 through PRs
#59–#62, Package 4 through PRs #63–#67, and Package 5 through PRs #68–#73.
Git ancestry checks proved every exact head of the 15-PR structural sequence
#59–#73 is an ancestor of this checkpoint.

The last canonical publication evidence for that checkpoint reported 994
tests, Pyright with 0 errors, successful hosted validation on Python 3.11,
3.12, and 3.13, and successful CodeQL. This is repository publication
evidence, not deployment or live Telegram/provider acceptance.

## Invariants for every package

- Preserve all public CLI/config imports and observable Telegram behavior unless
  a separately reviewed contract change explicitly says otherwise.
- Keep one SQLite transaction owner for atomic admission, job, material, session,
  and outbox operations. Do not introduce per-domain connections or an ORM.
- Do not rewrite released migrations or change schema for code organization.
- Keep fixed service allowlists and argv-only process control. No configurable
  command or unit-name execution surface is added.
- Keep acceptance assertions independent of production formatters where they
  verify user-visible output; a test must not validate a formatter with itself.
- Use characterization tests before moving behavior. Run focused checks during
  iteration and one final canonical publication gate on the exact clean commit.
- No deployment, daemon restart, credential change, live provider probe, or live
  Telegram E2E belongs to these refactors.

## Package 0 — validation and documentation ownership

State: complete (PR #56).

Acceptance:

- documentation, release/config contracts, formatting, and lint fail before
  whole-project typing and the complete test suite;
- the focused profile is visibly partial and cannot accept canonical-only
  selectors or silently replace canonical validation;
- canonical validation retains the history scan, Pyright, complete test
  discovery, release lock, metadata, configuration, and documentation gates;
- the publication hook still validates a clean exact `HEAD` and independently
  runs the complete gate; hosted CI trusts no local receipt;
- repository guidance names one owner for requirements, evidence, procedures,
  testing commands, rationale, and release summaries instead of repeating the
  full behavior in each document.

Do not add a validation receipt until repeated pushes of an unchanged commit
are a measured bottleneck. The remaining saving does not currently justify the
tool, policy, lockfile, worktree, and external-policy invalidation surface.

## Package 1 — acceptance runtime seam and stateful fakes

State: complete (PR #57).

Extract the local operational mechanics used by live acceptance before moving
the seven-step scenario itself:

- one read-only state probe for job membership/status and material cardinality;
- one fixed-unit service supervisor with explicit active-state capture and
  restoration;
- stateful fake implementations that record actions and allow named failures.

Keep scenario order, markers, result names, timeout behavior, and the single
restoration boundary in `acceptance_actor.py`. Replace ordered mock tuples such
as `side_effect=(True, False, ...)` with state assertions that describe the
service lifecycle.

Acceptance:

- all existing acceptance-actor tests remain behaviorally unchanged;
- regressions cover an initially inactive unit, failure after worker stop,
  failure during Controller restart, and restoration failure without losing the
  original scenario failure;
- state access is read-only, bounded, schema-aware, and emits no paths or row
  content in artifacts;
- no production service is contacted by repository tests.

## Package 2 — isolate the P0/P1 live scenario

State: complete (PR #58).

After package 1 establishes stable seams, move the fixed seven-result scenario
to a dedicated module. If shared dataclasses must move, place only the config,
error, and result contracts in a dependency-neutral module and re-export their
existing names from `acceptance_actor.py`.

The new scenario receives an explicit context containing the Telegram client,
validated actor configuration, state probe, and fixed service supervisor. It
must not receive arbitrary prompts, SQL, service names, or commands from
configuration. Avoid a plugin framework, registry abstraction, or generic DI
container.

Acceptance:

- existing configuration and CLI entry points remain compatible;
- the composite check still emits exactly the seven documented result names,
  stops at the first failure, and restores initially active services;
- provider-content, album cardinality, FIFO, restart idempotency, over-limit
  notice, context/quota, and read-only command tests remain independent;
- `acceptance_actor.py` retains orchestration/report ownership and no reverse or
  runtime-only import cycle is introduced.

## Package 3 — Controller vertical seams

State: complete (PRs #59–#62).

Do not split `service.py` by arbitrary line ranges. First map the dependencies
and characterize one vertical behavior, then extract only that seam. Preferred
order:

1. deterministic `/status`, `/accounts`, and `/model` command orchestration;
2. Telegram input normalization and route/admission decisions;
3. album/material collection and durable job admission;
4. prepared-result publication coordination.

The Controller remains the lifecycle facade. Extracted components receive the
minimum existing collaborators and return typed decisions; they do not open
state, load credentials, or construct provider adapters themselves.

Acceptance for each seam:

- focused tests instantiate the extracted behavior without constructing the
  full Controller;
- existing integration/fault-matrix tests prove the facade wiring;
- command paths remain model-free and cache-only where required;
- admission retains project/root/session/lane validation and idempotent offset
  behavior inside the existing transaction boundary.

One seam is one PR. Do not combine all four extractions.

## Package 4 — state domain facades on one connection

State: complete (PRs #63–#67).

Gradually group `state.py` operations behind internal domain facades while
retaining the existing `HubState` public surface and single SQLite connection:

- incoming materials;
- provider jobs and leases;
- outbox/progress delivery;
- sessions and writer ownership;
- runtime health.

Start with incoming materials because its invariants and focused tests are
already explicit. A facade may receive the owned connection and transaction
context; it must not commit, migrate, back up, or close independently. Move
queries and local row conversion together so behavior is not split across two
owners.

Acceptance:

- admission that binds job and materials remains one transaction;
- fault injection at every existing boundary retains rollback behavior;
- migrations, backup/open lifecycle, and public state method signatures remain
  unchanged;
- no generic repository base class or query-builder layer is introduced.

## Package 5 — explicit worker execution phases

State: complete (PRs #68–#73).

Make the existing worker lifecycle visible without inventing another durable
state machine. Isolate pure or narrowly effectful phases for:

1. lease validation and execution-root/lane revalidation;
2. material preparation and provider-input construction;
3. provider invocation;
4. result/artifact preparation;
5. atomic result commit and cleanup;
6. conservative failure or indeterminate classification.

Durable statuses and allowed transitions remain owned by the existing state
layer. The worker must not replay an ambiguous provider invocation, and Telegram
delivery failure must never repeat provider work.

Acceptance:

- existing abrupt-process and handled-failure matrices remain green;
- focused tests can exercise pre-invocation, accepted, partial, completed, and
  commit/cleanup failures separately;
- no new background probe or live inference path is added.

## Package 6 — bounded maintenance utilities

State: deferred.

Only after measured, repeated maintenance friction, consider:

- an explicit documentation-contract command that updates one existing numbered
  section digest, prints the change, and remains read-only by default;
- a documented source-to-focused-test map for common modules;
- validation timing comparison from required runs.

The test map is iteration guidance, never an automatic proof that unselected
tests are irrelevant. Digest update must not create sections, alter requirement
IDs, or treat a new digest as product approval.

## Delegation and review

Use agy Gemini Flash helpers when available, otherwise Luna High helpers, for
read-only discovery, dependency maps, repetitive file searches, test-output
triage, mechanical non-overlapping edits, and first-pass review. The primary
agent owns architectural boundaries, security and transaction judgments, final
diff review, canonical validation, commit, publication, and merge. Helpers must
not receive private operator context, credentials, deployment state, or live
mutation authority.

## Explicit non-goals

- no ORM, generic repository framework, event bus, plugin system, or DI
  container;
- no big-bang rewrite of Controller, state, worker, or acceptance actor;
- no async conversion for its own sake;
- no released-migration cleanup or schema renumbering;
- no shared constant that makes a live visible-output assertion validate the
  same formatter that produced the output;
- no claim that repository refactoring accepts a deployed installation.

## Closure evidence

- Published checkpoint:
  `a00ab09994d552e83fba470f745539cb88052e63` on `origin/main`.
- Package-to-PR mapping: Package 0 #56; Package 1 #57; Package 2 #58;
  Package 3 #59–#62; Package 4 #63–#67; Package 5 #68–#73.
- The exact heads of PRs #59–#73 are all ancestors of the published checkpoint.
- Last known canonical result: 994 tests and Pyright with 0 errors.
- Hosted publication result: Python 3.11–3.13 and CodeQL successful.

## Remaining triggers

- Package 6 remains deferred until repeated digest-maintenance, focused-test
  discovery, or validation-timing friction is measured and documented.
- Deployment, live acceptance, release-tag debt, and worktree or branch
  housekeeping remain separate operational work. They are not closure evidence
  for this refactoring plan.

## Reopening conditions

Reopen this plan only when measured maintenance evidence shows that one of its
deferred utilities is warranted, or when a regression demonstrates that an
extracted boundary no longer preserves the invariants above. New feature work,
an isolated large file, or a line-count change alone is not sufficient. Any
reopened package must name its current state, last verified revision, next
trigger, owner, bounded scope, and new closure evidence.
