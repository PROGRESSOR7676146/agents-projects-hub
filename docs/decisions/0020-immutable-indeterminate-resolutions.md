# ADR 0020: Immutable operator resolutions for uncertain work

Status: repository implementation; deployment acceptance pending
Date: 2026-09-12

## Decision

Schema 23 adds `provider_job_resolutions`, keyed one-to-one by provider job. An
operator may classify a terminal `indeterminate` job as `acknowledged`,
`superseded`, or `externally_completed`. The annotation records only the fixed
classification and its timestamp. It does not change the provider job, deliver
an answer, authorize replay, or contain free-form private task text.

Resolution is append-only. Repeating the same classification is idempotent;
attempting to replace it must fail. This preserves the original uncertainty and
error evidence while allowing passive audits to distinguish unresolved work
from cases the operator has already handled.

## Migration and rollout

The additive table uses the provider job as a foreign-key parent and shares the
existing transactional migration, backup, and integrity checks. Runtime
rollback after migration requires an artifact whose maximum supported schema is
at least 23. Therefore schema support is released as a separate revision before
the operator command: that revision is the compatible rollback artifact for
the behavior revision.

## Evidence

Migration tests cover schema 22 to 23, preservation of an existing
`indeterminate` job, the exact table shape, and SQLite integrity. Behavior tests
cover terminal-state validation, idempotency, conflict rejection, unchanged job
evidence, CLI behavior, and passive audit reporting without replay.
