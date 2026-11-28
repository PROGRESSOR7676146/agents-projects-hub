# ADR 0117 — Exact inputs for nightly Hub admission

Status: accepted for the read-only source slice. Executor and installation
acceptance pending.

## Decision

Use a versioned private admission ticket derived from one explicitly selected
deployment plan. A successful external backup can authorize the next stage
only when its archived manifest binds the ticket's exact installed inputs.
A catalog entry, passive consumer check or receipt from a different run
supplies insufficient evidence.

The installed manifest and its SQLite snapshot describe the baseline. The
rollback in the ticket is the release running at preparation time. It can
differ from the rollback recorded by that installed manifest. The next manifest
and SQLite snapshot come from the deployment window and belong to its outcome.
Predicting either digest would add no evidence.

The archive owner continues to verify and adopt the installed inputs before
backup under its own lock. A callback inside Hub's restart window would couple
the two locks and make archive repinning part of restart recovery. Keep that
ownership with the archive runner and use a passive downstream readback after
convergence. A mismatch pauses future scheduled changes and preserves both
sets of pinned files.

The first profile admits a forward release with the same schema, primary
configuration, loaded service topology and registry. It accepts the normal
release-path rewrite and worker configuration projection in the saved plan.
Changed unit templates and Claude CLI store workflows require separate evidence.
[ADR 0094](0094-one-command-hub-deploy.md) owns deployment and rollback.
[ADR 0115](0115-owner-requested-day-deploys.md) owns timing. The
[runbook](../operations/NIGHTLY_HUB_ADMISSION.md) describes the read-only entry
and the evidence required of the executor before scheduled activation.
