# ADR 0091: Workstation shows the project without a trust scan

Status: accepted owner direction; source candidate, installed acceptance pending.
Date: 2026-11-07
Owner: repository owner. Implementation owner: Hub maintainer.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Amends point 4 of [ADR 0081](0081-claude-workstation-host-view.md).

## Context

ADR 0081 stopped trust-scanning the read-only host but kept the entry-by-entry
walk of the writable project and linked-worktree Git directories. That walk
refuses any multiply linked regular file, special file, depth over 128 and more
than 100,000 entries. It exists for the confined `editor` and `developer`
profiles, where the namespace is part of the boundary.

In `workstation` the namespace is a guard against mistakes, not a boundary:
the whole host is readable and Docker is root-equivalent. The walk still
refused ordinary developer checkouts. `uv` links virtual-environment files to
its cache by default, and a large repository with local caches, acceptance
trees and nested worktrees passes 100,000 entries easily. Claude then refused
every turn before inference, while Codex ran in the same directory.

## Decision

1. In `workstation`, the project tree and the linked-worktree Git directories
   are shown without the per-entry walk. Hardlinks, special files and large or
   deep trees admit, as for Codex workspace-write.
2. A bind alias between these writable sources and the session HOME is still
   refused, by comparing pinned mount coordinates (device and path inside the
   filesystem) instead of walking either tree. The comparison applies to every
   profile.
3. These checks are unchanged: descriptor pinning and recheck, mount-table
   nested-mount and native-filesystem checks for every writable root, path
   overlap with runtime, code, toolchain and private authority, the private
   authority guard, the session-HOME walk, runtime and toolchain immutability
   scans, and the read-only `.hub/work` mount.
4. `editor` and `developer` keep the full walk.

## Consequences

Claude works in the same checkouts as Codex. A write through a hardlink can
change a file outside the project, including a file whose other name is
mounted read-only, such as a shared package cache or a `.hub/work` entry. This
is the same mistake risk Codex already carries; it is not hostile-agent
confinement. Writable linked-worktree Git metadata can affect the other
worktrees of the repository, as before.

Without the walk, a mount created inside the project after the mount-table
check is no longer caught by a second inspection. Neither check closed that
race before; pinning does not freeze a subtree's mount topology.

## Alternatives considered

- Repoint Claude to a clean worktree: a lane binds the whole topic, so it also
  moves the topic's main agent. It hides the mismatch instead of removing it.
- Keep the walk and drop only the hardlink and size rules: it keeps a launch
  cost that grows with the tree and protects nothing the trust model needs.
- Weaken the scan for every profile: the confined profiles rely on it.

GPT Astra was consulted before this decision. It agreed with the change and
asked to keep the alias check and to document the hardlink consequence.
