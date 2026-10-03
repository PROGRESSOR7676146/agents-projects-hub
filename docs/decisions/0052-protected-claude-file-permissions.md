# ADR 0052: Protected human permissions for Claude file tools

Status: accepted implementation choice; deployment/live acceptance pending.
Date: 2026-10-03.

## Context

Ordinary tlive Claude approvals cannot distinguish a human callback from policy
trust or automatic grants. Its standard plugin also adds continuation hooks.
The owning boundary is [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Decision

Keep native Claude invocation, FIFO, root exclusion, stop and recovery in the
existing Hub worker. Add a PermissionRequest-only native hook and a short-lived
worker socket adapter, without another daemon or database. The native process
sees only that socket. A pinned additive tlive 5.3.1 source patch owns a separate
pending map, authenticated IPC namespace and original human callback metadata;
the legacy router, dashboard grants and continuation channel cannot settle it.
Distinct request/result HMAC keys bind opaque exact payloads and a new daemon
epoch. A complete bounded preview precedes Allow once or Deny; large or sensitive
inputs are refused rather than silently masked or truncated.

Schema 38 adds payload-free launch/request records. ClaudePermissionJournal
owns each immediate transaction, verifies the current binding at preparation
and consumption, commits Allow once before replying, and revokes pending
requests on cleanup. A recovery lease or restarted worker cannot reuse an old
launch. Lost native delivery never restores a consumed receipt. Source-topic
waiting notices use the existing durable Hub sender and become stale when the
request or binding is no longer current.
Session mode and the session-home digest are persisted with the native identity;
switching between text-only and file-tool storage requires a fresh `/new` session.
Concurrent hook requests are denied without queuing; an abandoned native hook
revokes its human wait. Tool-result events are validated and discarded, with
separate bounded raw/event budgets and the existing visible-text limit retained.

Linux bubblewrap provides explicit readonly root-owned runtime/code mounts, an exact
writable project with readonly Git metadata, a dedicated provider session home,
disposable temporary directories and the single per-turn socket. Its own
readonly procfs belongs to the separate PID namespace; host processes and
private filesystem endpoints are absent. Shared network access permits the
explicit CPA loopback route: reachable loopback/abstract socket services still
require their own authentication. No shell, MCP, external plugins, skills or
child-agent tool is exposed. Read tools may be permitted natively without a
prompt; filesystem confinement is their boundary, not a claim that every read
requires a human callback.

Hosted argv use restricted mode, empty setting sources, strict empty MCP,
disabled skills, manual permissions and no prompt fallback. Safe mode is kept
for the text-only default; hosted hooks use explicit settings in the restricted
filesystem view. The known built-in AGENTS.md and telemetry plugins are explicitly disabled,
and unexpected plugin/tool/permission metadata fails the stream guard.

## Ownership and consequences

Integration owner: Hub maintainer. Worker owns invocation/process cleanup;
the socket thread owns its separate SQLite connection. Journaling never chooses
Allow: it validates a signed human decision. Released DDL remains append-only;
the existing migration/backup transaction owns schema 37 to 38 upgrades.
Runtime settings, mount ownership and guarded invocation are extracted into
focused modules; the adapter's turn entry point no longer needs a hotspot exception.

The optional mode requires a separately installed immutable runtime and hook,
ordinary Git metadata directory, explicit private mount exclusions and compatible
Linux namespaces. It has no unsandboxed fallback. Input previews above 2,500
characters are presently denied; a faithful larger review flow is future work.
The patch is source-hash gated and must be re-reviewed when tlive changes. If
maintaining it becomes a deep or complex fork, replace the approval UI with a
native Hub permission host rather than weakening the boundary.

The trusted worker and tlive share symmetric receipt keys. Filesystem confinement
keeps those keys out of this Claude process; mode 0600 alone cannot protect them
from another unconfined process with the same UID. Live activation is blocked
until OS separation of all untrusted principals from keys/state/endpoints is
proven. Dedicated custody and asymmetric result signing are the next trigger
before a read-only advisor or broader provider threat claim. The current slice
does not attest to that deployment separation. Runtime symlink targets and ACL
write access are checked, but concurrent trusted host changes remain outside
the path-pinning guarantee.

This is a limited implementation slice. Full lead/advisor roles, local transfer,
saved-session discovery and subscription/no-paid-fallback acceptance remain
separate. Offline fixtures do not attest to deployed Telegram or provider behavior.
See the [runbook](../operations/CLAUDE_FILE_PERMISSIONS.md).
