# ADR 0008: Resident Codex proxy lifetime belongs to systemd

Status: accepted  
Date: 2026-09-03

## Context

The optional multi-auth wrapper starts a loopback runtime-proxy helper before
starting the official Codex app-server. Its generic helper lifecycle includes
detached-idle and maximum-lifetime reapers intended to prevent helpers from
outliving short CLI launchers.

A systemd-managed app-server is different: it is resident, its wrapper remains
the service main process, and the unit cgroup already owns cleanup. A helper
reaped while the app-server remains alive leaves the Unix control socket
connectable but removes account rotation. Process state and socket readiness
therefore continue to look healthy while provider traffic cannot reach the
configured loopback upstream.

## Decision

- The systemd drop-in disables the multi-auth detached-idle and absolute
  lifetime reapers for this resident launch.
- The generic idle deadline is moved far beyond an ordinary unit lifetime as a
  compatibility guard for wrapper versions that misclassify a live owner.
- systemd remains responsible for terminating every process in the unit cgroup.
- The existing connectable-socket readiness probe and tlive `After=` ordering
  remain unchanged.
- Monitoring continues to probe the runtime proxy independently. It must not
  automatically restart a shared app-server because that can disconnect an
  active Codex or tlive repair session.

## Consequences

- A resident proxy is no longer reaped by a short-command cleanup heuristic.
- Normal stop, restart, failure, and reboot cleanup remain bounded by the unit's
  cgroup and `KillMode` policy.
- An already degraded running unit requires one operator-coordinated restart;
  installing the drop-in alone deliberately does not disconnect current users.
