# ADR 0007: Shared Codex socket readiness requires a connection

Status: accepted  
Date: 2026-09-03

## Context

An optional rotating Codex app-server and a tlive companion may share the
default Unix control socket. systemd ordering alone is insufficient if the
upstream unit reports ready merely because the socket path exists. An abrupt
host or WSL stop can leave a stale socket inode on disk. A consumer then starts,
cannot connect, launches its own app-server, and races the rotating server for
the same path.

## Decision

- The rotating app-server's activation waits for a successful bounded Unix
  stream connection, not `test -S` or file existence.
- tlive is ordered after that activation with `After=` only.
- Neither unit uses `Requires=` or `BindsTo=`; failure of one recovery channel
  does not stop the other.
- The readiness probe is a small tested product command installed with Hub.

## Consequences

- A stale inode cannot falsely release the dependent startup ordering.
- A healthy rotating server owns the shared socket before tlive decides whether
  to adopt it or launch a companion.
- A genuinely failed optional server delays tlive only for the bounded readiness
  timeout, after which tlive remains free to start independently.
