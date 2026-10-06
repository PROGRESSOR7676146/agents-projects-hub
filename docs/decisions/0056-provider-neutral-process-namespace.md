# ADR 0056: Provider-neutral pinned process namespace

Status: implemented extraction candidate; publication review and installed custody pending.
Date: 2026-10-07.
Owner: Hub maintainer.

## Context

The Claude file-tool wrapper already owns pinned mount validation, bounded
tree scans, immutable-runtime checks and descriptor cleanup. Copying those
controls for a future advisor would create two owners of the same security
mechanism. Prompt restrictions cannot establish the read-only boundary required
by [REQ-WRITER-013](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Decision

Extract those mechanisms into `process_namespace.py`. A frozen
`NamespaceRuntime` captures trusted runtime identities once and rechecks them
at each launch; a frozen `ProcessNamespaceConfig` declares explicit project,
session-home and private sources. Its initial defaults are read-only project
access, private networking and no permission socket. The initial private
profile refuses a permission socket. Namespace-owned HOME, proc, dev and run
destinations cannot be shadowed by ordinary mounts; a narrow project below
disposable tmp, the readonly Git overlay and consistent readonly runtime nesting
remain supported.

Keep `FileToolSandboxConfig` as the frozen Claude facade with its existing
constructor, public attributes and `wrap()` signature. It explicitly selects
project read/write, readonly Git, shared networking for the existing loopback
route, and the existing one-inode permission socket. Claude owns its environment
allowlist and fixed config directory. The generic core validates explicit
environment entries and fixes HOME, XDG, TMPDIR and PATH without inheriting host
environment. Alias the neutral error at the old import location, retaining
catch behavior and original exception causes. The error class name is now
`NamespaceError`; no state or protocol relies on the former Python class name.

Keep `claude_mount_pins.py` unchanged: its inode/type identity, descriptor
adoption and final pin recheck already have one neutral owner. Move test helper
patches to their defining module rather than retaining facade callback plumbing.
Existing invocation, process cleanup, native permissions, signed receipts,
worker/state transactions and result publication keep their owners.

The second consumer is an offline Python witness. It uses authorized material
inside a readonly project and a separate writable session HOME; it exercises a
parent and exec child without a provider, permission host or inference route.
Required strict CI includes it alongside the existing Claude and Codex witnesses.
The [testing guide](../testing/README.md#live-acceptance-boundary) owns its checks
and evidence limits.

## Consequences and next trigger

There is one namespace validator and builder. Both access modes retain bounded
scans and project/HOME inode-intersection rejection. Pinning does not freeze
directory contents or exclude concurrent unconfined host mutation. The second
consumer is not a production advisor, launcher for other providers, complete
snapshot, deployment inventory or host-wide custody attestation.

Private networking cuts the current loopback inference route. Next design a
bounded inference transport and authorized-material boundary before wiring any
advisor; do not reconnect host authority services or silently widen this profile
to regain inference. Role changes, bounded review state, local/helper/MCP paths
and the installed acceptance in [ADR 0053](0053-claude-custody-reference-deployment.md)
remain separate work. No runtime feature flag, service change or live activation
is introduced by this extraction.
