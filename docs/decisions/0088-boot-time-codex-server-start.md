# ADR 0088: Boot-time external Codex server start

Status: proposed
Date: 2026-11-03
Owner: repository owner
Amends: [ADR 0073](0073-tlive-approval-evidence-and-server-custody.md) (no automatic start)

## Context

ADR 0073 gives the pinned Codex app-server its own unit with `Restart=no` and
no install target. Every start needs a fresh operator `plan` receipt that
asserts all native clients are closed, followed by an explicit unit start.
After a host reboot the server therefore stays down until the owner repeats
that procedure. Until then the Codex workers fall back to stdio, and tlive
cannot attach. The owner has said that routine upkeep must not depend on their
attention. A forgotten manual step after every reboot is exactly that kind of
dependency.

The manual receipt protects one premise: no native Codex client or server
runs while a new server takes custody of the socket. Right after boot, the
service manager can enforce that premise itself, so a person does not have to
assert it.

## Decision

The work proceeds in three phases. Phase 1 is implemented. Phases 2 and 3
need the owner's acceptance of this ADR, because phase 2 amends REQ-OPS-005.

1. **Phase 1: see the outage (implemented).**
   - The doctor's `codex_socket` check now passes only when kernel `SOCK_DIAG`
     evidence shows a listening stream socket bound to the current filesystem
     inode. A socket file left by a crash or reboot no longer passes.
   - The monitor raises `codex_app_server_unavailable` once per episode. It is
     a warning when a stdio fallback exists and an error otherwise.
   - The check never connects, deletes or starts anything.
2. **Phase 2: a narrow rule for leftovers of a previous boot.** Today every
   reboot leaves the socket path behind, so any boot start would refuse.
   - A reviewed cleanup may remove only the exact manifest socket path, its link
     and its lock file. It applies only when all of the following hold:
     - the path's change time is earlier than the current boot time;
     - kernel evidence shows no listener;
     - a connect attempt fails with `ECONNREFUSED`;
     - the same-UID inventory finds no Codex server or client.
   - It uses the descriptor-relative removal of the existing stale-socket
     runbook. Anything else still refuses.
   - This amends REQ-OPS-005 for that one case.
3. **Phase 3: one owning unit, two authorization modes.**
   - The existing app-server unit gains a `boot` mode next to the manual
     receipt mode. A second competing unit is not added.
   - One lock serializes both modes.
   - `boot` proves closed clients by `Before=` ordering and a host `/proc`
     inventory, not by assertion.
   - `boot` records the boot ID in an `O_EXCL` marker under
     `$XDG_RUNTIME_DIR`. Any existing marker refuses, so a later crash in the
     same boot still needs the manual `plan` + `run`.
   - Clients wait on a readiness barrier: the listener is verified by
     `SOCK_DIAG` custody of the started process before tlive or Codex workers
     start.
   - Every ADR 0073 gate stays: artifact and release pins, existing-server
     refusal, and exact idle state.
4. **Refusal keeps Codex unavailable, not substituted.**
   - When a start is refused for ownership or unknown work, the workers do not
     fall back to a stdio writer on the same root.
   - The unit stays failed, and the phase 1 alert tells the owner.
   - Nothing retries, kills or repairs.

## Consequences

- Phase 1 alone already removes the silent case: after a reboot the owner
  learns that Codex and tlive approval are down.
- After phases 2 and 3, a clean reboot with no unknown Codex work brings the
  server back without the owner. Every case the manual procedure refuses is
  still refused and alerted.
- No Telegram, provider or LLM call, no replay and no new authority is added.

## Open questions before acceptance

- **Codex socket lifecycle.** Which files Codex 0.166 leaves after a SIGTERM or
  a reboot (socket, link, lock), and whether a supported setting avoids them.
  Phase 2 removes exactly that set and nothing else.
- **Login timing.** Without lingering, the user manager starts at login, not at
  boot. The marker and ordering still hold. "Boot" then means the first
  user-manager start.
