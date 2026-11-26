# ADR 0115: Day deploys only on the owner's request; other releases wait for the deploy window

Status: accepted (owner decision, 2026-11-25)
Date: 2026-11-26
Owner: repository owner
Amends: [ADR 0087](0087-autonomous-project-upkeep.md) rule 3 (when to deploy),
the [Autonomous upkeep](../../AGENTS.md#autonomous-upkeep) bullets,
[the upkeep sweep](../operations/UPKEEP.md) and
[maintenance policy](../product/MAINTENANCE.md#19-maintenance-and-change-policy) item 18

## Context

ADR 0087 lets every assigned session deploy the newest release on its own as
soon as its gates hold. In practice that moved the installation during the
owner's working day, after nearly every release:

- each deploy restarts services while the owner may be using them;
- an external backup consumer that pins Hub inputs by hash saw them change
  between its nightly runs and had to re-pin before it could back up again;
- the owner learned that the installation changed only from the report.

The owner wants to decide when the live installation changes in the daytime,
and lets routine releases wait for a quiet scheduled window in which the
backup and the deploy run one after the other.

## Decision

1. **Day deploys need an explicit request.** Outside the installation's
   scheduled deploy window, an agent deploys only when the owner explicitly
   asks to install a named release, or explicitly asks to make a named change
   live on this installation. A request to implement, test, merge, release
   or tag something is not a request to deploy. When it is unclear which
   release the owner means, the agent asks.
2. **Other releases wait for the window.** The window is an unattended,
   model-free deploy entry that the installation's scheduler starts. It is
   planned and does not exist yet. Until it does, a release that nobody asked
   for waits for an owner request. Agents never imitate the window by
   deploying at night on their own initiative.
3. **No urgency exception.** A security fix waits for the request as well.
   The agent tells the owner promptly about the exposure and the ready
   release, and recommends installing it.
4. **Recovery stays immediate.** Rolling back to the rehearsed artifact after
   a failed check, and restarting a failed component of the currently
   accepted release under the idle rule, are not deployments. Neither moves
   the installation forward, and neither permits a backup restore or another
   reserved step.
5. **The gates are unchanged.** Every deploy, requested or in the window,
   keeps all conditions of ADR 0087 rule 3: the newest release tag on `main`
   moving forward only, green exact-head checks, not a failed target, a kept
   backup, a rehearsed compatible rollback, the idle check, and the
   reservations on credentials, configuration meaning and default-off
   features.
6. **External backup consumers.** After a change to any input such a consumer
   pins, the agent runs the consumer's read-only check in the same session and
   puts the result in the report. A consumer that re-pins itself needs no
   message. When the check is not ready, the report names its reason.
7. **Waiting is reported.** A ready release that waits is listed in the
   session report with a recommendation, and recorded in the private handoff.

Which window an installation uses, and its consumer's check command, are
deployment facts. They stay in the private handoff, never in Git.

## Consequences

- The installation changes in the daytime only when the owner asked for it,
  and the backup sees one consistent state per night.
- Released fixes may wait, possibly for days, until the owner asks or the
  unattended window exists. The session report keeps them visible.
- The unattended window entry is separate planned work. Its own ADR defines
  the admission ticket, the receipt and the exit codes for the scheduler.
- Unchanged: the rest of ADR 0087, the deploy tool's checks and recovery, and
  every owner-reserved step.
