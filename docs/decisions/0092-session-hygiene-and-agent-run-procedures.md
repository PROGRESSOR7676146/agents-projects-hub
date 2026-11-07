# ADR 0092: Sessions clean up after themselves; agents run every procedure

Status: accepted
Date: 2026-11-07
Owner: repository owner
Amends: [ADR 0087](0087-autonomous-project-upkeep.md) (the elevated-privilege
reservation and rule 4)

## Context

By November 2026 the repository had 340 worktrees, 185 local and 162 remote
branches, many old deployment kits and a requirement digest file at its size
limit. About 200 worktrees were clean and merged, and 130 remote branches were
already in `main`. Each session created a worktree, a branch, a scratch clone
or a kit for its task, and none was required to remove them. ADR 0087 lets
any session remove clean merged leftovers, but it made cleanup somebody
else's later sweep, so nobody did it.

The owner also asked once more for commands to run: a `sudo` install, a
reboot, a `/new`. The owner does not perform technical procedures and does not
want to. A step that needs the owner to type a command makes the owner the
bottleneck and is easy to get wrong.

## Decision

### Sessions remove what they create

1. **Where things go.** A session creates worktrees only under
   `.hub/worktrees/<task>-<date>` of the primary checkout, one per task, never
   as sibling directories or inside caches. Temporary files go to the session
   scratch directory. Deployment kits go to one `rollout-<version>` directory
   in the private operations cache.
2. **Before the session ends**, or as soon as the work is merged or abandoned,
   the session removes its own worktrees, local branches and remote branches
   whose head is in `main`, and stops the background processes it started. It
   inspects tracked, staged, untracked and ignored files first: work products
   and evidence are moved to the private operations cache or recorded, never
   deleted with the worktree.
3. **Kits.** After a successful deployment, keep the kit of the running
   release and of its rollback release. Remove older kits whose release is
   neither running nor a recorded rollback.
4. **Anything kept** past the session (unfinished work, evidence, a long-lived
   worktree) is recorded in the private handoff with its purpose and the event
   that ends it. A leftover that is not recorded and is older than seven days
   is fair game for any session's upkeep sweep, under the cleanliness checks of
   ADR 0087 rule 4.
5. **Merged remote branches** are deleted on merge. The repository setting
   that deletes head branches automatically is enabled, so this needs no
   session action.
6. The session report says what the session removed and what it kept, and why.

### Agents run every technical procedure

1. The owner makes decisions; agents run the commands. An agent never asks the
   owner to run a command, copy output, edit a file, restart a service, reboot
   for a technical reason or test by hand. The same holds for every step that
   ADR 0087 reserves for the owner: the owner decides, and the agent carries it
   out once the owner has said yes. Live acceptance and live model probes stay
   owner-authorized; once authorized, the agent runs them, for example through
   the dedicated Telegram acceptance actor, instead of asking the owner to send
   messages.
2. Only steps that need the owner as a person stay with the owner: signing in
   to an account or issuing a credential, answering an approval request, and
   actions on the Windows host or hardware outside the Linux installation. The
   agent prepares them so that one short action is enough.
3. A procedure that still needs an owner command is a defect. The agent removes
   the need, for example with a supported automation or a standing permission,
   and records the defect in the [owner-step defects](../operations/OWNER_STEP_DEFECTS.md) list
   until it is gone. This replaces the ADR 0087 reservation of elevated-privilege
   steps: the root-owned runtime install is such a defect. The user-owned
   sealed runtime of [ADR 0089](0089-prepared-release-switcher.md), which the
   owner approved on 2026-11-05, is the planned replacement.
4. When the agent's own tool permissions refuse a routine step, such as
   removing a clean merged worktree, the agent stops, as ADR 0087 requires, and
   asks the owner once for a narrow standing permission instead of asking for
   each action.

## Consequences

- Leftovers no longer pile up; the cost moves to the session that created
  them, which knows best what is safe to remove.
- Removing worktrees and branches is still bounded by ADR 0087 rule 4: only
  clean, merged and inspected items, so no commit and no evidence is lost.
- The owner sees decisions and reports, not instructions. Until the user-owned
  runtime is adopted, a release that changes the root-owned runtime still waits
  for one privileged step; it is tracked as a defect, not as a routine.
