# ADR 0076: Confined linked-worktree Git metadata

Status: source implementation; publication and installed acceptance pending.
Date: 2026-10-26. Owner: Hub maintainer.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Decision

Extend the existing namespace to a standard linked Git worktree. Ordinary roots
keep their existing mount/access policy. Validate and pin the worktree's `.git`
file, matching metadata backlink and standard `commondir` relationship without
following source symlinks. Unsupported layouts fail before productive invocation.
The worker still revalidates the registered root/lane; Git pointers are not new
root-selection authority.

Mount only this worktree's metadata, shared objects/refs and existing reference
logs. Config, hooks, info and optional packed-refs/shallow files are read-only;
the common directory itself, primary checkout/index/HEAD and sibling worktree
metadata are absent. The common directory is synthetic and read-only. Developer
may edit this worktree's index/HEAD and shared objects/refs; editor gets read-only
Git sources. Pointer files remain read-only in both profiles. Normal edit/add/
commit/status/log operations are supported. Repository administration requiring
common-directory writes, such as pack-refs, config changes and worktree creation,
is intentionally unsupported. Shared references/history are repository-wide:
this does not establish isolation of Git history or independent ref namespaces.

Every exposed source retains descriptor pinning, private-authority/provenance,
native-filesystem and hardlink checks. Extra Git directories also undergo the
bounded tree scan. No entire neighboring repository or operator directory is
mounted to make Git work. Existing host mutation/trusted same-UID limitations
remain under ADR 0072; pinning is not a contents snapshot or a host writer lock.

Hide project-contained `.hub/work` behind an empty read-only namespace mount,
because operational work may contain backup/review material. Incoming materials
and artifact staging retain their ordinary project mounts. Operational material
must still not be copied elsewhere into the provider-visible project.

This supersedes only the linked-worktree exclusion in ADRs 0052, 0056, 0067,
0072 and 0075. No new session/profile, permission grant, replay, root rebind or
unconfined fallback is introduced. Native Bash still requires fresh human
permission. The provider-neutral namespace owns mounts; the permission host
and worker keep invocation, receipt and cleanup ownership.

## Evidence and closure

`tests.test_namespace_worktrees` uses actual `git worktree add`, negative pointer,
source-link and private-path cases, and a real provider-free Linux namespace
edit/commit witness. It checks unchanged primary/sibling HEADs and hidden
operational material. This is offline source evidence, not native model or
Telegram delivery acceptance. Next: exact-source gates/review and narrow
installed namespace acceptance. Existing failed jobs are not replayed.
