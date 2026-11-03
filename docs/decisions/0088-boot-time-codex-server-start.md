# ADR 0088: Boot-time external Codex server start

Status: accepted
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

The owner chose to automate the start after a reboot and accepted the narrow
link cleanup (2026-11-03). Phase 1
shipped first; phases 2 and 3 complete it.

1. **Phase 1: see the outage.**
   - The doctor's `codex_socket` check fails when the kernel `SOCK_DIAG` dump
     shows no listening stream socket on the current filesystem inode. A socket
     file left by a crash or reboot no longer passes. When that evidence is
     incomplete or unavailable, the state is `unverified` and the check keeps
     its previous pass.
   - The monitor raises `codex_app_server_unavailable` once per episode for a
     leftover path without a listener. A fully removed path alerts too when
     configuration expects the external server: there is no stdio fallback, or
     tlive is configured. The alert is a warning with a stdio fallback and an
     error otherwise.
2. **Phase 2: the previous boot's link.** Codex 0.166 keeps the real socket in
   a private directory under `/tmp` and links to it from the manifest socket
   path in the Codex home. A reboot clears the socket and leaves the link, so
   every start would refuse.
   - The first start attempt of a boot may remove that link, and only that
     link, when all of the following hold:
     - it is owned;
     - its target is gone;
     - its change time predates the current boot;
     - the same-UID inventory finds no server or native client.
   - The removal is descriptor-relative and rechecks the link's identity.
   - Sockets, regular files, live links and links from this boot stay, and the
     pins then refuse the start. This amends REQ-OPS-005 for that one case.
3. **Phase 3: one owning unit, two authorization modes.**
   - The existing app-server unit gains an opt-in `[Install]` target. A second
     competing unit is not added; systemd serializes starts of the one unit.
   - A fresh operator receipt authorizes any start, as before.
   - Without one, only the first start attempt of a boot may proceed. An
     `O_EXCL` marker under `$XDG_RUNTIME_DIR` records the attempt before any
     check, so a refused or crashed first start, and every later start in that
     boot, needs the manual `plan` + `run`.
   - That attempt requires user lingering and proves closed clients itself,
     failing closed: no same-UID process may run any Codex or tlive
     installation, name a pinned artifact or carry an `app-server` argument.
     The link removal follows the static manifest and pin checks.
   - Every Codex consumer unit (Controller, workers, Codex bot, tlive) is
     ordered `After=` the server unit, without depending on it.
   - Both modes then pass every ADR 0073 gate: artifact and release pins,
     existing-server refusal and exact idle state.
   - A `ready` step waits for kernel listener evidence, without connecting,
     before ordered clients start.
4. **A refusal starts nothing else.** The unit stays failed, nothing retries,
   kills or repairs, and the phase 1 alert tells the owner. Keeping Codex
   workers off their stdio fallback after a refusal is not part of this ADR.

## Consequences

- Phase 1 alone already removes the silent case: after a reboot the owner
  learns that Codex and tlive approval are down.
- After phases 2 and 3, a clean reboot with no unknown Codex work brings the
  server back without the owner. Every case the manual procedure refuses is
  still refused and alerted.
- No Telegram, provider or LLM call, no replay and no new authority is added.

## Limits

- **Lingering.** Without lingering, `$XDG_RUNTIME_DIR` is cleared at the last
  logout, so "first start of a boot" would mean "first start after login". The
  boot mode therefore refuses without lingering; the manual receipt still works.
