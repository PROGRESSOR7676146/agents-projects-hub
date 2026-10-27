# ADR 0078: Usable confined developer tools and explicit consultations

Status: accepted owner-requested increment; source candidate, installed acceptance open.
Date: 2026-10-27. Owner: repository owner. Implementation: Hub maintainer.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Decision

Extend the existing developer namespace with an explicit readonly toolchain and
PATH. Keep the native CLI, worker, writer lease and human permission host. There
is no host executor, nested Codex runtime, new daemon, database or queue.

Trusted runtime/hook mounts retain immutable root-owned validation. Separately
configured development tools may be owned by root or the worker, but cannot be
writable by others. They remain readonly inside the namespace, descriptor-pinned,
bounded, on supported native filesystems and disjoint from private authority,
project, session HOME and control runtime. Hardlinks and special entries refuse.
This relies on the accepted trusted-host model of ADR 0072; readonly mounting
does not freeze bytes against another host process or certify project code.

The namespace may explicitly select visible Git hooks, validation Python,
author-policy file and commit identity. No whole host HOME or global credential
store is exposed. Disable implicit upstream setup and automatic gc/maintenance
in this launch: common linked-worktree config is readonly and sibling metadata is
hidden. Branch creation from a remote ref must not switch files and then fail
writing implicit tracking config. Explicit repository administration stays
unsupported for linked roots. Use a separately registered independent clone
without hardlinks/alternates if that limitation obstructs useful work; preserve
changes and do not silently retarget an existing native session.

Add `hub-consult`: one explicit stdin question to an allowlisted CPA Responses
route, with pinned requested model/effort and expected response model. No tools,
conversation writer, repository discovery, session or native OAuth extraction.
The command returns only complete visible assistant text, never reasoning or raw
errors. Bound input/output/time, disable proxies/redirects/retry and fallback,
and report incomplete/unknown outcomes without automatic replay. Parent stop
owns the process group; killing the caller does not establish remote cancellation
or refund. Consultation is available only through an explicitly authorized Bash
command or the owner's temporary Bash consent; it is not passive monitoring.

Local configuration must record a separately verified no-paid-fallback CPA route
and inference-only credential. That declaration and a loopback URL do not prove
billing or entitlement. The client allowlist is not a quota/security boundary
against project code that can read that same inference credential. Private
signing/management credentials remain absent. Source review remains independently
Opus-only under the existing runbook; this command is not a review scheduler.

## Ownership and acceptance

`namespace_toolchain` owns explicit values/environment; `process_namespace` owns
pinning/mount checks and launch. The small `consult` command owns its single HTTP
request and cleanup; it owns no Hub state transaction, approval or result replay.
Existing worker, permission-host, native process and state cleanup owners remain.

Offline acceptance includes actual branch creation, Python/Node execution, a
build output, hook rejection followed by successful commit, readonly tools and
unchanged main/sibling worktrees/config. Add HTTP fixtures for exact request,
visible-only parsing, redirect/proxy refusal, wrong model, incomplete output,
timeouts and no retry. Existing private-path, Deny, stop and grant regressions
remain required. These checks are source evidence, not installed acceptance.

The bounded growth of `ProcessNamespaceConfig._build` is intentional mount wiring,
not a second invocation owner. Further launch/environment responsibility must
reassess extraction. Owner: Hub maintainer; next review: 2026-11-10; reopen on any
new endpoint, tool capability, launch mode or lifecycle branch. Configuration and
consultation logic are already separate cohesive modules.

Next: exact-source Opus review/canonical/CI, merge, then separately authorized
installation and the real edit-test-build-commit-consult cycle. Closure remains
open until those installed results are observed. Full delegation, MCP/plugins,
steer, local transfer/images, personal project chat and five-session rollout
retain their own scope and ordering.
