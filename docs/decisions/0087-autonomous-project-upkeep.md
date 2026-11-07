# ADR 0087: Autonomous project upkeep

Status: accepted
Date: 2026-11-02
Owner: repository owner
Amends: the deployment, service-restart and branch-deletion reservations in
[AGENTS.md](../../AGENTS.md#review-merge-and-scope) and the per-update approval
in the [Hub restart procedure](../operations/HUB_RESTART.ru.md)
Amended by: [ADR 0092](0092-session-hygiene-and-agent-run-procedures.md)
(sessions clean up after themselves; the elevated-privilege reservation becomes a defect)

## Context

The owner cannot watch routine upkeep and does not want to. The project rules
delegated publication and merging to agents, but kept deployment, service
restarts and branch deletion behind separate approval. Nothing told an agent to
close a superseded pull request or to resolve stale Hub state. So each session
either asked for approval one action at a time or left the upkeep for later.

Upkeep that waits for the owner fails in predictable ways:

- pull requests whose work is already merged in another form, or superseded,
  stay open and hide the ones that matter;
- unresolved uncertain jobs and stale delivery records can block the idle
  check of an unrelated service change;
- dependency updates wait with green checks;
- a release with a forward migration but no prepared compatible rollback
  leaves a backup restore, which loses later work, as the only way back.

Asking is not safer. It only makes the owner the bottleneck, and the debt then
creates its own risk.

## Decision

Routine upkeep is a standing duty of every agent session that the owner
assigns to this repository or its installation. Agents do it without asking,
as part of their ordinary work, and report it afterwards. The owner keeps only
a narrow set of steps that are destructive, cannot be undone, need credentials
or change what the installation trusts.

This decision covers development and administrative agent sessions, the
trusted helpers of [ADR 0072](0072-trusted-personal-installation.md). It does
not give authority to Hub-hosted provider turns, Hermes incident cards or
background timers. Background automation stays model-free. "Automatic" means
that every assigned session performs the upkeep. It never means a scheduler
that starts model turns, and no platform automation merges or deploys on its
own.

### What agents do on their own

1. **Pull requests.** Close a pull request when all of its commits are
   reachable from `main`, or when a merged pull request superseded it. Close it
   without a comment, so the owner is not flooded with notifications. Keep its
   branch until rule 4 applies. A closed pull request can be reopened. Fix CI
   and rebase pull requests the agent owns. A pull request that carries unique
   unmerged work is neither closed nor discarded: the agent finishes it under
   the merge rules when it is within scope, and otherwise lists it in the
   report.
2. **Dependabot.** Merge a Dependabot pull request when every exact-head hosted
   check is green and the diff changes only dependency declarations, lock files
   or pinned action versions. Merge it like any other pull request: recheck the
   head and base right before merging and pin the expected head. Do not enable
   auto-merge, which could merge a later head nobody checked. A version bump
   has no authored source, so it needs no independent model review. For a
   major version bump of a runtime dependency (`[project].dependencies`), the
   agent first reads the upstream release notes for breaking changes. Any pull
   request that also changes authored code follows the ordinary review rule.
   Dependabot holds new version updates for seven days, so a release that
   upstream pulls soon after publishing does not reach `main` automatically.
   Security updates are not delayed.
3. **Releases, deployment and rollback.** The deployment target is the newest
   release tag on `main`, and deployment only moves forward from the release
   the installation runs. An older release is installed only as a recorded
   rollback. Deploy the target to the existing installation, and restart its
   services through the service manager for that purpose, when all of these
   hold:
   - its exact-head hosted checks are green;
   - it is not recorded as a failed target (see below);
   - a consistent backup of the state was taken before the change, and is kept;
   - a compatible runtime rollback artifact exists, and its rollback was
     rehearsed on a copy of the populated state
     ([REQ-OPS-011](../product/PERSISTENCE_AND_RECOVERY.md)). Rolling back
     therefore needs no backup restore;
   - the idle check of the [restart procedure](../operations/HUB_RESTART.ru.md)
     passes. The agent waits for an active turn to finish and never
     interrupts it;
   - the release changes no credentials and no configuration meaning, and
     enables no default-off feature.

   After the change every required component must report the exact revision
   ([REQ-OPS-009](../product/PERSISTENCE_AND_RECOVERY.md)). If it does not, or
   any check fails, the agent rolls back to the rehearsed artifact, confirms
   that every component reports the rollback revision, and reports both
   results. It records the release as a failed target in the private handoff,
   and later sessions do not deploy it again until a newer release or a
   recorded fix addresses the cause. If the rollback itself cannot be verified,
   the agent stops every further change, preserves the evidence and tells the
   owner at once instead of waiting for the session report. The same idle rule
   allows restarting a failed component to recover it. `pkill` stays forbidden.
4. **Branches and worktrees.** Delete a branch only when its head is reachable
   from `main`, so no commit is lost. Never delete the default branch, a
   protected or release branch, a recorded long-lived branch, or a branch with
   an open pull request or an active session. Remove a worktree only
   after inspecting its tracked, staged and untracked state
   ([maintenance policy](../product/MAINTENANCE.md#19-maintenance-and-change-policy)),
   and only when it is clean and merged.
5. **Stale Hub state.** Resolve terminal uncertain jobs that are older than
   24 hours and have no live process. Use only the supported resolution
   command and the conservative `acknowledged` classification; `superseded`
   and `externally_completed` need concrete evidence. Settle stale unknown
   deliveries without resending them, through the supported dispositions in
   [queue recovery](../operations/QUEUE_RECOVERY.md). Reset the session of an
   acceptance, test or canary project through supported commands only when it
   is verifiably idle: no queued or running job, no local writer, and no other
   session working with it. Otherwise leave it and list it. Never edit
   SQLite, delete state files, replay a job or resend a message. Leave a
   session bound to a topic the owner uses, and list it in the report.
6. **Records.** After any state change, update the private handoff and, where
   the change is reusable, the public plan and status documents.

### What stays with the owner

- Restoring data from a backup, and any step whose only way back is a backup
  restore. A forward migration without a rehearsed compatible rollback is such
  a step.
- Credentials and accounts: `claude setup-token`, provider login, token or bot
  token rotation, allowlists, and enabling paid or extra usage.
- Steps that need elevated privileges, such as a `sudo` install of a
  root-owned runtime. The agent prepares and checks everything else and asks
  for that one step. [ADR 0092](0092-session-hygiene-and-agent-run-procedures.md)
  makes each such step a tracked defect to remove.
- Permanently deleting data: databases, backups, session histories, project
  roots, unmerged branches or commits. Also rewriting shared history,
  force-pushing `main` and moving an existing tag.
- Changing what the installation trusts or how it behaves for the owner:
  project registration or disablement, security profiles, sandbox and
  approval policy, and activating a default-off feature or any activation that
  its own requirement reserves for the owner.
- Live Telegram acceptance and live model probes, which stay owner-invoked.
- Answering any approval or permission request. Agents never do this.
- Product decisions and anything outside the assigned task scope.

When an agent cannot tell whether a step can be undone, it treats the step as
reserved, asks the owner once and recommends an option.

### Always

- Take a backup before changing state and keep a verified way back.
- Treat a refusal by the permission layer, a sandbox or a gate as a stop. Report
  it and ask; never look for a way around it.
- Report afterwards, once per session, not once per action. The report says
  what was done, which revision runs, where the rollback pointer is, and what
  is left for the owner. It goes to the channel where the owner gave the task.
  Deployment facts go to the private handoff and never to Git.
- Keep the existing gates: canonical validation, privacy scan, exact-head
  hosted checks, branch protection, independent review of authored changes,
  evidence levels and passive-only background monitoring.

## Consequences

- The owner reads reports instead of answering requests. Upkeep happens in
  every session, so debt cannot quietly pile up again.
- An agent can close or deploy something by mistake. The risk is bounded:
  closed pull requests can be reopened, branches survive until their commits
  are in `main`, and a deployment always has a rehearsed rollback and a kept
  backup.
- Merging Dependabot updates without a model review accepts some supply-chain
  risk. Exact-head CI, the pinned-head merge, the seven-day cooldown and
  release notes for major runtime bumps limit it.
- Unchanged: owner-pressed incident analysis ([ADR 0084](0084-impact-classified-incidents-and-owner-paging.md)),
  the owner-confirmed plan of the planned stack updater (REQ-OPS-015, whose Hub
  releases keep their own procedure), every human approval boundary and every
  requirement that names a separate owner authorization for a specific
  activation. Maintenance policy item 6, which prefers reversible failure over
  autonomous repair, governs the product's runtime code, not agent upkeep.
