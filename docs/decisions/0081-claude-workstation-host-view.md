# ADR 0081: Claude workstation profile sees the whole host

Status: accepted owner direction; source implementation, installed acceptance pending.
Date: 2026-10-28
Owner: repository owner. Implementation owner: Hub maintainer.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Supersedes [ADR 0080](0080-claude-workstation-profile.md) before installation.
Keeps the trust model of [ADR 0072](0072-trusted-personal-installation.md).

## Context

ADR 0080 widened the Claude view to listed system and source trees and masked
`.env*` and `.hub/work` entries. Independent review showed that such masks are
a best-effort enumeration of a live tree: entries created during or after
launch, deeper copies and hardlinks stay readable, and closing the gaps needs a
snapshot per launch. The owner judged that the masks do not protect much but
hinder work, repairs and maintenance. This installation trusts friendly, capable
agents and limits damage from mistakes with frequent backups, budgets and
provenance, not with isolation; it cannot defend against a hostile agent anyway.

## Decision

The explicit `workstation` profile matches Codex workspace-write:

1. The pinned host root is bound read-only first; namespace-owned `/dev`,
   `/proc`, `/tmp` and `/run` cover it. The whole host, including credentials
   and Hub/tlive authority, is readable. There are no visible-root lists, masks
   or secret inventories to maintain.
2. Persistent writes reach only the project, its admitted Git metadata (as in
   `developer`) and the session HOME, which is mounted at its own host path
   because a read-only root cannot hold a new mount point. The project
   `.hub/work` is created when absent and mounted read-only, so a mistake
   cannot corrupt Hub work products.
3. The rootful `/run/docker.sock` is restored into the fresh `/run` when
   present, as is a resolver target under `/run` such as systemd-resolved's
   stub file, and supplementary groups are kept, so Docker access matches other
   agents. Rootless Docker and other sockets under `/run` or `/tmp` are absent;
   sockets elsewhere in the host view stay reachable. Docker is root-equivalent: the
   namespace is a guard against mistakes, not a boundary.
4. The host root is shown, not trust-scanned, so it stays out of the private
   mount and nested-mount checks. Private paths must still not overlap a
   writable or trusted runtime mount.
5. Native Bash runs without a per-command human decision; the permission host
   still validates each request, job, writer and root, and journals the Allow.
   Stop, the single writer, no uncertain replay and immutable runtime and hook
   code are unchanged. Networking stays shared.

Push, deployment and service changes belong to the owner by policy and prompt
instruction, not by a technical barrier.

## Consequences

Claude has the same practical reach as Codex and the same residual risk. The
`editor` and `developer` profiles keep their behavior and evidence. Damage
limits move to operations: Hub state and project backups with tested restore,
CPA and subscription budgets, the parallel-root limit, and recorded agent,
model and revision provenance for each turn.
