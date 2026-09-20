# Refactoring plan

Status: accepted incremental backlog  
Date: 2026-09-20

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

## Starting checkpoint

The current `maintenance/fail-fast-validation` branch contains the first
package as commit `d10aadf`: cheap contracts precede expensive validation,
focused development checks are distinct from canonical acceptance, validation
prints stage durations, documentation ownership is explicit, and the pre-push
hook remains uncached and fail-closed. This commit is not integrated merely
because it exists locally. The next agent must review it, run its focused tests
and mandatory privacy/history gate, publish a PR, wait for the complete CI and
CodeQL result, and merge only when the exact revision is green.

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

State: implemented on the current branch; review and integration pending.

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

Only after the structural packages demonstrate repeated friction, consider:

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

## Handoff sequence

1. Finish package 0 on its existing branch and merge it independently.
2. Create a fresh branch from the resulting `origin/main` for package 1.
3. Before each package, assign helpers a read-only dependency/test map and have
   the primary agent approve the exact boundary.
4. Land characterization tests before movement, then perform the smallest
   mechanical extraction that makes them pass.
5. Run the focused profile during iteration, the mandatory privacy/history gate
   before commit, the installed publication preflight on push, and hosted CI and
   CodeQL for the exact revision.
6. Record only changed evidence or procedure. Do not copy this plan into status,
   requirements, ADRs, or changelog.

