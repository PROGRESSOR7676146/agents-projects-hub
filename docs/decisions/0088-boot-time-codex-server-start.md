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

1. **A separate opt-in boot unit.**
   `agents-projects-hub-codex-app-server-boot.service` runs
   `codex_server_service boot` and has `WantedBy=default.target`. It keeps
   `Restart=no`, `KillMode=mixed`, `SendSIGKILL=no` and `UMask=0077`. The
   existing manual unit and `plan`/`run` stay unchanged. Enabling the boot
   unit is a deliberate installation step, and only one of the two units may be
   enabled.
2. **Clients closed by ordering plus inventory, not by assertion.**
   - The boot unit is ordered `Before=` tlive, the Controller, and every Hub
     worker unit.
   - The `boot` action inventories every same-UID process through host `/proc`.
     It refuses when any process executes the pinned Codex binary or the pinned
     tlive daemon, or carries an `app-server` argument. An unreadable process
     refuses, as `refuse_existing_servers` already does.
3. **First start of this boot only.**
   - Before exec, `boot` creates a marker in `$XDG_RUNTIME_DIR` (a tmpfs the
     system clears on reboot), with `O_EXCL` and mode 0600. The marker records
     `/proc/sys/kernel/random/boot_id`.
   - An existing marker refuses the start. A crash or stop later in the same
     boot therefore still needs the explicit manual `plan` + `run`, as ADR 0073
     requires.
4. **Every other ADR 0073 gate stays as it is.**
   - `boot` calls the same `prepare` checks: artifact pins, the clean immutable
     Hub release pin, the existing-socket refusal, the existing-server refusal,
     and the exact idle state, including unsettled controls and local writers.
   - It never deletes a socket, kills a process, migrates state or repairs
     anything.
   - A stale socket left by an unclean shutdown still refuses the start.
     Handling it stays the owner's documented incident procedure.
5. **A refusal changes nothing compared with today.** If `boot` refuses:
   - the unit is left failed;
   - Codex workers keep their existing stdio fallback;
   - the existing monitor reports the missing server or tlive attachment once.

   The refusal does not retry, does not start anything else and does not
   block the other Hub units.

## Consequences

- After a clean reboot with no unknown Codex work, the server comes back
  without the owner.
- Every case the manual procedure would refuse is still refused. The owner
  sees the refusal through the existing alert path and resolves it with the
  current runbook.
- REQ-OPS-005 is unchanged: no socket deletion, no killing, and refusal on
  active or unknown work, unsettled controls, unowned servers or sockets, and
  mismatched artifacts.
- This ADR adds no Telegram, provider or LLM call, no replay, and no new
  authority.

## Open questions before acceptance

- **Clean shutdown.** Does Codex unlink its listening socket on SIGTERM? If it
  does not, every reboot leaves a socket path behind. The boot start would
  then always refuse, and the value of this ADR depends on a separate,
  explicitly reviewed rule for stale sockets from earlier boots, for example:
  socket ctime earlier than the current boot time, `connect()` refused, and no
  same-UID server. That rule would change REQ-OPS-005, so it is out of scope
  here.
- **Login timing.** When lingering is disabled, the user manager starts at
  login, not at boot. The marker and ordering still hold. "Boot" then means
  the first user-manager start.
