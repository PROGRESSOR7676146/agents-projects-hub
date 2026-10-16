# Conservative schema54 rollback candidate

State: compatibility candidate under validation; owner: Hub maintainer.
Base: `a6fddf3941f3ec56b623a7fa73dfb657d8dc9c36`.
Next trigger: independent exact-candidate review and offline artifact rehearsal.
Closure: open until reviewed compatible wheel and retained-evidence checks pass.

This is a distinct fallback artifact, not the late-receipt feature release. It
retains the base sender/queue policy while admitting the additive schema54 DDL.
It preserves immutable attempts and receipt evidence without projecting or
deleting them. Its older producers create conservative `legacy` outboxes by
default. It may again park late responses as unknown and retain notice barriers;
that degraded behavior is an explicit rollback limitation.

Never restore an old database over newly accepted work. Coordinate every process
on one compatible clean revision; keep a fresh SQLite-consistent backup, exact
manifest and configuration. This source candidate authorizes no activation.

Architecture exception: migration registration grows by only an imported DDL
constant and ordered entry. The separate schema module owns SQL; the existing
runner retains backup, transaction and failure rollback. No historical migration,
provider invocation, approval or sender lifecycle changes. Hub maintainer owns
this bounded exception; review by 2026-11-16 or when another schema, ledger
maintenance or new send authority is proposed. Exact-source review is required.

The worktree owns only this fallback. Inspect tracked, staged and untracked
state after integration before any cleanup.
