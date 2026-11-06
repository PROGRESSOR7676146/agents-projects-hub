# ADR 0089: A standalone prepared-release switcher

Status: source candidate; independent review and deployment integration pending
Date: 2026-11-05
Contracts: [REQ-OPS-010/011/015](../product/PERSISTENCE_AND_RECOVERY.md),
[REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Decision

Use a standard-library-only `scripts/stack.py` for the filesystem and service
transition of already prepared releases. Keep acquisition, package-manager
installation and release-specific checks with their owning tools. A single
private manifest names exact version directories, coordinated service groups
and passive gates. `plan` runs no commands and changes nothing.

Bind confirmation to manifest contents, current links, both release trees and
the preceding switch record. Before any service action, save the entire
before/after mapping durably in one private record. Atomic link changes need
no per-link intent records or sequence counter: after interruption, each link
must still name one of that record's two targets. Any other target refuses
recovery. An unfinished operation requires explicit rollback, never resuming
the forward switch automatically.

Stop a group's consumers before changing any of its links, then start in the
declared dependency order and check health. Restore groups in reverse order
after failure. Rollback verifies the retained old release and its compatibility
gate before stopping anything; it never restores a database or other data.
One exclusive lock serializes apply and rollback. Keep only the latest switch
record; rollback of older windows is outside this tool.

Services may use `launch` to resolve `current` once and exec a canonical
release path. Future imports then remain on that release when the link changes.
The independent script must itself be pinned outside managed release trees.

## Boundaries

This is an offline-tested transition primitive, not completion of ADR 0048's
owner controls, acquisition, drift detection or Hermes self-update. It adds no
Hermes/model tool and no background worker. Live application still requires the
owning release procedure, backup, exact compatibility and health checks, and
deployment authorization. The owner controls of REQ-OPS-015 remain required
for any future integration.

The owner authorizes user-owned Hub runtime and trusted hook versions so that
preparing later releases need not use sudo. Keep the root-owned default and add
only an explicit local list of version-directory/digest pins. Verify the sealed
tree at construction and before each confined launch. User ownership permits
the owner to change host permissions; it is not hostile-same-UID/admin isolation.
Read-only namespace mounts, private-path and writable-overlap guards stay in
force. Bubblewrap itself remains an OS-owned prerequisite. This decision changes
the source trust policy; it does not activate or migrate any installation.

Use the same structured tree digest for the standalone switcher and namespace
pins: newline-delimited compact JSON records of relative path, mode, type and
file-content digest or literal symlink target. Keep implementations independent
so recovery needs no Hub imports; a conformance test checks the common format.

## Consequences

The switch mechanism stays independent of a broken Hub, Hermes, Python virtual
environment or package manager. Preparing a release remains a separate,
component-specific step. Gate commands are trusted private configuration and
must be passive, bounded and preserve state; they are not a sandbox or an
approval system. A symlink flip is atomic, a multi-component window is not;
the single record makes a partial window recoverable.

The [runbook](../operations/PREPARED_RELEASE_SWITCHER.md) owns manifest syntax,
invocation, stop conditions and the deployment integration still required.
