# Routine upkeep sweep

Status: active
Last updated: 2026-11-02

[ADR 0087](../decisions/0087-autonomous-project-upkeep.md) owns what agents
do on their own and what stays with the owner. This page is the procedure.
Run the sweep at the start of every assigned session, after the read order in
[AGENTS.md](../../AGENTS.md), and again before ending a session that changed
state. Keep it bounded: a sweep that finds nothing to do should take minutes.

## 1. Repository

1. List **all** open pull requests. Page through the whole list; the first page
   of a listing is not the whole set.
2. For each pull request, decide one of:
   - all commits reachable from `main`, or superseded by a merged pull
     request: close it without a comment;
   - Dependabot, only dependency declarations, lock files or pinned actions
     changed, exact-head checks green: merge it, or enable auto-merge. For a
     major runtime dependency bump, read the upstream release notes first;
   - owned by this session, or in scope and ready: finish it under the
     [merge rules](../../AGENTS.md#review-merge-and-scope);
   - unique unmerged work outside the scope: leave it open and list it in the
     report.
3. Delete remote branches whose head is reachable from `main` or from a
   retained tag. Keep every other branch.

## 2. Installation

Work on the installation only when the session is assigned to it, and
coordinate with any other session that is changing it. Use only passive
checks: `status`, `doctor` and the audits. Never run a live model probe.

1. Read the private handoff and recheck its claims against the live system.
2. Inspect worktrees for tracked, staged and untracked state. Remove only the
   clean ones whose head is merged.
3. Resolve stale uncertain jobs and settle stale deliveries as
   [ADR 0087](../decisions/0087-autonomous-project-upkeep.md) rule 5 allows,
   through the supported commands in [queue recovery](QUEUE_RECOVERY.md).
4. When `main` has a release tag that the installation does not run, deploy it
   if every condition of rule 3 holds. Follow
   [release preparation](IMMUTABLE_RELEASES.md) and the
   [Hub restart procedure](HUB_RESTART.ru.md). When a condition is missing,
   prepare it first. For example, publish the compatible rollback release and rehearse
   the rollback on a copy of the populated state. Ask the owner only when the
   missing condition is one of the reserved steps.

## 3. Report

Send one short report to the channel where the owner gave the task:

- what was closed, merged, deleted, resolved or deployed, as counts with
  exceptions named;
- the exact revision every component runs, and the highest evidence level;
- the backup and rollback pointer, described without private paths;
- what is left for the owner, each item with a recommendation.

Then rewrite the private handoff to the current state.
