# ADR 0080: Claude workstation profile — wide read view without secrets

Status: accepted owner direction; source implementation, installed acceptance pending.
Date: 2026-10-28
Owner: repository owner. Implementation owner: Hub maintainer.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Amends the mount allowlist of [ADR 0052](0052-protected-claude-file-permissions.md)
and the per-invocation Bash rule of [ADR 0075](0075-claude-personal-developer-profile.md)
and [ADR 0077](0077-temporary-claude-bash-consent.md) for one new explicit profile.

## Context

The confined Claude namespace was designed for a global custody goal: prove that
the model cannot reach Hub/tlive authority. Its view is an explicit allowlist:
the project, a session HOME and pinned runtime roots. ADR 0072 later withdrew
the global goal and accepted that Codex workspace-write, OpenCode, Antigravity
and Hermes turns can read authority files. Claude kept the stricter allowlist.

Live acceptance on 2026-10-28 showed the cost. A developer commit failed in 24
of 320 test modules because ordinary system data was absent (time zones, a
user database, MIME tables), and project plans stalled on hidden operational
files and missing tools. Each gap needs its own exception, so the allowlist
does not converge. Every Bash command also needs a human decision unless the
owner grants `Bash until restart`.

## Decision

Add an explicit `workstation` profile beside `editor` and `developer`. Omission
keeps `editor`; absence of the configuration keeps text-only. The existing
worker, permission host, journal, queue and native CLI are reused.

The namespace widens its view from the project to the system and source
trees. Credential directories stay absent.

1. An explicit `visible_roots` list is visible read-only. Only `/usr`, `/etc`
   and `/opt` may be named whole; other entries must be narrow trees. `/proc`,
   `/dev`, `/sys`, `/run`, `/tmp`, `/var`, `/mnt` (WSL Windows drives), `/root`,
   `/boot`, `/home` and a whole user home always refuse. The existing separate
   PID and IPC namespaces, fresh `/proc`, minimal `/dev`, `/tmp` and `/run`,
   dropped capabilities and closed descriptors remain, so host sockets under
   `/run`, `/var/run` and `/tmp` are absent. Visible trees are not scanned for
   sockets: keep service sockets out of listed source trees.
2. The operator home is not mounted as a whole. An explicit list of source trees
   (for example the operator's project directory) is visible read-only, with
   the canonical targets of `.hub/work` and case-insensitive `.env*` entries
   masked at each tree's top level and in its immediate children, where
   checkouts live, and in the writable project. Every path is resolved, so an
   alias into any mounted tree masks its target; masks under a masked directory
   are dropped, and a source tree with a nested mount refuses. The masks are
   best-effort enumeration of a live tree, not a snapshot: an entry created or
   swapped during or after launch, such as a new Hub lane's `.hub/work`, a deeper
   copy or a hardlink under another name stays readable. Private operator notes
   (handoff, review receipts) live in configuration directories that are never
   mounted; a copy placed in a listed tree is readable. Credential directories such as `~/.config`, `~/.local`,
   `~/.cache`, `~/.ssh`, provider homes and tlive are never mounted. A host
   inventory on 2026-10-28 found tokens, keys, Telegram sessions and
   state-database copies throughout those directories, so a per-file deny-list
   inside the home would not converge.
3. Persistent direct filesystem writes go only to the project and the Git
   metadata ADR 0076 admits; the session HOME and `/tmp` are disposable. This
   limits direct writes, not effects through host services. Ordinary project
   Git metadata is writable, as in `developer`, so
   changed hooks and build scripts are reviewed before the owner or another
   agent runs them on the host.
4. Supplementary groups cannot be dropped without privilege, so a group such as
   `docker` still applies to visible files. Its root-equivalent socket lives
   under `/run` and is therefore absent. bubblewrap keeps no-new-privileges. A
   separate UID is not adopted (ADR 0072).
5. The allowlist of visible trees is the primary control. The mandatory
   private-path list is a second check: launch fails if a visible tree contains
   or lies inside a listed path, overlaps the session HOME, or lies inside the
   project. It covers at least Hub state and configuration, tlive keys and
   sockets, permission signing keys, bot and Telegram session credentials, the
   CPA and consultation keys, provider auth files, Git/GitHub credentials, SSH
   keys, backup passwords and the private operator files.
6. Native Bash inside this view runs without a per-command human decision, like
   Codex workspace-write. The native hook still sends each Bash request to the
   permission host, which validates input and current job, writer and root
   binding, then journals an Allow without a human card. Background and
   sandbox-bypass options, MCP, skills, plugins and agent children stay
   unavailable.
7. Human-only approval, first-valid-answer, stop, no uncertain replay, the
   single writer and immutable root-owned runtime and hook code are unchanged.

The profile does not supply configured credential locations, so push and
deployment through them are unavailable. That push, deployment and service
changes belong to the owner is policy, not a technical guarantee: a service
reachable over the shared network, or through a socket placed in a listed tree,
acts with its own authority and authentication.

## Consultation and resolved questions

GPT Astra (high) reviewed this design on 2026-10-28, assuming a possibly hostile
agent. Adopted: explicit system-tree list, masks in the writable project, hidden
operator notes, no-new-privileges, the allowlist as primary control, and no
nested user namespaces. The namespace test modules therefore still cannot run
inside this profile; a commit gate for them is a separate decision. Not adopted,
per ADR 0072: a separate UID or VM. The owner kept shared networking, as today
and as for Codex: loopback services without their own authentication stay
reachable, an accepted residual risk of this installation.

GPT-6.1 Sol (high) reviewed the implementation. Fixed: masks missed checkouts
reached through a symlinked child, nested masks broke a valid launch, and a
nested mount under a source tree escaped the mount check. Narrowed instead of
fixed: masks enumerate a live tree once, so a race-free promise would need a
snapshot per launch; the boundary is the allowlist, not the masks.

GPT Astra (high) reviewed the fixes. It found the narrowed wording inconsistent
across documents and the prompt, and mask paths not canonical when a symlink was
an intermediate component or targeted another mounted tree. All masks are now
resolved against every identity mount before nesting checks, and every
statement is best-effort. Owner acceptance of the remaining race, deep-copy and
hardlink risk is pending; review does not replace it.

## Consequences

Claude gains the same practical reach as Codex for reading and running project
work. The protected property narrows to what matters in a trusted personal
installation: the model cannot read credential directories that are never
mounted, directly write the filesystem outside the project and its admitted Git
metadata, or approve for the owner. It is not hostile-agent or same-UID isolation. New source trees
and secret locations need installation review: a secret copied into a visible
source tree is readable unless a launch-time mask happens to cover it. `editor` and `developer`
keep their current behavior and evidence.
