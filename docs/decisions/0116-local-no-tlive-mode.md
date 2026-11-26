# ADR 0116: An installation may switch tlive off when nothing asks a human through it

Status: accepted (owner decision, 2026-11-25); implemented in source. Switching
an installation is a separate deployment step.
Date: 2026-11-26
Owner: repository owner
Amends: product principle 7 and REQ-OPS-005 (a local exception). Unchanged:
[ADR 0017](0017-tlive-approval-only-hub-sessions.md), REQ-WRITER-005 and
REQ-SEC-002.

## Context

tlive carries human decisions that Hub must not take: Codex approvals under
`on-request` and the human permission receipts of confined Claude profiles
(`editor`, `developer`, `workstation`). It also mirrors interactive native
Codex sessions to Telegram. The product therefore treats tlive as one of two
independent recovery channels, next to Hermes, and Hub expects it to run:

- the config requires tlive's config file when the recovery plane is on;
- the doctor probes it, and the monitor raises `tlive_recovery_unavailable`
  and polls its approval surface;
- the deploy descriptor lists `tlive.service` among the units that must be
  active.

An installation that runs Codex with the local `full` profile (ADR 0096) and
Claude with `host` (ADR 0095) asks no human anything through tlive, so tlive
has nothing to carry for Hub. Its daemon still runs, takes memory and must be
kept healthy, and a stopped tlive keeps an alert open. The owner of such an
installation wants tlive off without removing tlive support from the product,
which installations with human approvals still need.

## Decision

1. **Switch.** A top-level Hub config key `tlive_enabled`, a boolean that
   defaults to `true`. `false` switches tlive off for this installation.
2. **Refusal.** Loading the config refuses `false` unless
   `codex_permission_profile` is `full` and no confined Claude file-permission
   profile is configured (absent or `host` is fine). Hub runs Codex work only
   under the current installation profile and holds jobs pinned to another, so
   the installation-wide value is the one that matters.
3. **Effects when off.**
   - Hub never reads `recovery_plane.tlive_config_path`. The key may stay, so
     switching back changes one value.
   - The doctor shows `tlive` as switched off by owner configuration, without
     probing it, and omits `recovery:tlive`.
   - The monitor stops polling the approval surface and retires its open
     episode instead of keeping it as unobserved. It sends no tlive alert.
   - Hermes is then the only independent recovery channel. Losing it raises
     `recovery_plane_unavailable`, as losing both channels did before.
   - The external Codex app-server stays expected exactly as before, with its
     listener alert.
4. **The marker stays.** Every Hub Codex turn keeps the
   `TLIVE APPROVAL-ONLY SESSION` line of ADR 0017. If a compatible tlive runs
   while the config says off, for example after someone restarted it, the
   marker still keeps Hub threads approval-only, so tlive cannot offer a
   second writer for them. It costs a few tokens per turn.
5. **Reconfigurable.** Only the doctor and the monitor read the switch, and
   the monitor reads the config on every run, so `hub_deploy reconfigure` may
   change it on an accepted window. The installed runtime must know the key:
   an older release ignores it and still expects tlive.
6. **Procedure.** The [deploy runbook](../operations/HUB_DEPLOY.md#switching-tlive-off)
   owns the order. The descriptor change comes before a window, because
   `rollback` and `reconfigure` refuse a descriptor that differs from the one
   their window recorded. Switching off stops and disables only
   `tlive.service`; its package, unit files, build and bot token stay. The way
   back starts tlive and checks it before the switch returns to `true`, so Hub
   never declares a known-down channel required.

Hermes independence, the one-writer rule and every human approval boundary
stay unconditional.

## Consequences

- Such an installation runs one daemon fewer and has no permanent tlive
  alert. Its recovery plane is Hermes alone, and the monitor says so.
- Interactive Codex sessions that the owner starts outside Hub with
  `on-request` lose tlive's remote approval and Telegram mirroring; their
  approvals stay in the terminal. Hub cannot see such sessions, so the ADR
  documents the loss instead of detecting it.
- An installation that later wants human approvals must first switch tlive
  back on; the config refuses the combination otherwise.
- A rollback to a release without this ADR expects tlive again and alerts
  until it runs. A release rollback restores the config from before its
  window, so the way back is to start tlive first.
