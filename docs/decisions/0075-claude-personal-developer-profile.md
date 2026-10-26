# ADR 0075: Explicit personal Claude developer profile

Status: accepted scope; offline implementation candidate; publication and live acceptance pending.
Date: 2026-10-26. Owner: repository owner. Implementation owner: Hub maintainer.
Contract: [REQ-AUTH-009 and REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

The per-invocation-only Bash rule below is amended by
[ADR 0077](0077-temporary-claude-bash-consent.md). Earlier acceptance evidence
remains scoped to the original profile; temporary grants need their own checks.

## Decision

Add an optional `developer` profile to the existing protected Claude file-tool
configuration. Omission means `editor`; absence of that configuration still
means text-only. Reuse the native CLI, worker, permission host, namespace and
queue. There is no new executor, broker, shell-word security classifier or
provider-neutral agent framework.

Developer preauthorizes project file editing and makes ordinary project Git
metadata writable. Every native Bash invocation, including read-only commands,
requires a fresh exact human receipt through the protected PermissionRequest
host. Approval covers the command/program and its descendants inside the existing
namespace. Tests, scripts and Git hooks can modify project files. This is not
per-system-call approval, hostile-project isolation or a network firewall.
The supported installation remains personal and trusted under ADR 0072.

The editor's per-write permissions and read-only Git metadata remain its own
profile. Profile and session-home identity are pinned before invocation; changing
profiles requires `/new`, including after resume or rollback. The existing
`file_tools` storage bucket can retain a domain-separated developer home digest,
preserving editor digests and making an older editor executable refuse a developer
session. No new schema or reinterpretation of old grants is needed.

Only the six native file/Bash tools are in this increment. MCP, skills, external
plugins and agent children remain disabled. Native background and sandbox-bypass
options, unknown command input and incomplete or masked previews are denied.
Commands do not gain automatic grants, persistent rules or changed input from a
native permission suggestion. Private state/signing material stays outside the
namespace; runtime and hook stay immutable, with a separate session HOME and
exact allowlisted root/session binding. The single writer, stop, completion and
no-uncertain-replay owners remain unchanged. Ordinary Git roots are supported;
linked worktrees remain a later increment.

## Scope amendment and acceptance

This supersedes only the developer deferral and tool/Git restrictions in ADRs
0052, 0056, 0067 and 0072 for the explicit developer profile. It grants no live
activation and changes no other provider. Editor evidence is retained, not
relabelled as developer acceptance.

Before activation, prove the exact production argv/settings/host against a
bounded native offline fixture: edit, test and local Git cycle; fresh repeated
Bash Allow/Deny with denied effects absent; exact resume; stop, cleanup and
unknown-result preservation. Preserve negative namespace and receipt checks.
Then require paired source review, exact-source canonical/CI gates, compatible
rollback and separately scoped installed/human Telegram acceptance. A custom
prototype hook or unit test alone cannot establish those live results.

Current source checkpoint: integration base `74babf0` plus the developer candidate.
Focused policy/host/session/namespace tests and the pinned native offline
edit/test/Git and exact-resume fixture pass. The original editor fixture also
passes. The protected tlive source build includes fresh Bash Allow/Deny despite
legacy grants. Fictional worker/process tests cover developer stop and preserved
uncertainty; they do not establish native CLI stop or installed custody.
Next trigger: final source gates and independent review, followed by compatible
installation and separately scoped native/human acceptance.
Closure remains open until the bounded source/native/live criteria pass.
