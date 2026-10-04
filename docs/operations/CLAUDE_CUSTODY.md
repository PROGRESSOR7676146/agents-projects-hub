# Claude authority custody preparation

Status: preparation only; no infrastructure or live acceptance performed.
Source baseline: `6a848c7a326b35e4c3ea552339e8c5c3d7beba53`.
Integration owner: Hub maintainer. Last verified baseline is source evidence;
candidate changes require their own publication checks.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Design: [ADR 0053](../decisions/0053-claude-custody-reference-deployment.md).
Keep the existing [file-tool runbook](CLAUDE_FILE_PERMISSIONS.md) for its
runtime, mount, key, tlive and native permission procedures.

## Prepare a reviewable deployment bundle

Prepare the following privately before requesting any live action. Do not put
inventory, credentials, receipts or administrative identities in the checkout.

| Artifact | Required content and stop condition |
| --- | --- |
| Management map | Every host/guest admin, hypervisor/disk/snapshot/backup access path, and agent execution identity. Stop if any agent can use a management path. |
| Launch map | Productive start/resume, text-only CLI, shared/stdio app-server, local transfer, direct-message, helper, hook, MCP, recovery and capability-probe paths. Mark each trusted-only, technically confined, or blocked; a missing path blocks cutover. |
| Service map | Guest TCP listeners, pathname and abstract Unix sockets, system/user buses, forwarding and descriptor-passing services. Record intended agent exposure and authentication; unknown or authority-bearing exposure blocks activation. |
| Release bundle | Exact clean candidate, root-owned runtime/hook artifacts and hashes, dependency provenance, pinned tlive build, schema-compatible rollback wheel, private release manifest and consistent backup. |
| Data/identity map | Exact canonical roots, allowed roots, native session identities/modes/homes, leases and unresolved jobs. Any changed binding requires its existing explicit workflow; copying state is not a root migration. |
| Acceptance plan | Disposable project/topic, fictional OS sentinels, exact expected effects, bounded waits, cleanup ownership, success/failure evidence and no-inference checks. |

The current source does not provide a complete provider-domain launcher or an
automated host administration collector. This bundle is a prerequisite, not a
generated approval token. Preparing the bundle grants no access to live state.

## Automated offline rehearsal

Run the normal focused profile and the strict namespace corpus on a host whose
immutable system Python and bubblewrap fixtures are available:

```sh
python scripts/validate.py --profile focused \
  tests.test_claude_custody_rehearsal tests.test_workflows
HUB_REQUIRE_NAMESPACE_TESTS=1 python -m unittest -v \
  tests.test_claude_file_sandbox.ClaudeFileSandboxTests.test_namespace_denies_private_symlink_git_write_and_host_paths \
  tests.test_claude_permission_host_roundtrip.PermissionHostRoundtripTests.test_namespace_client_preserves_peer_gate_and_atomic_allow_deny \
  tests.test_claude_custody_rehearsal.ClaudeCustodyRehearsalTests.test_namespace_blocks_authority_aliases_and_privilege_but_shares_network
```

The last test first proves that an unconfined same-UID subprocess can read a
fictional mode-0600 key. Through the production namespace it then verifies
direct/symlink/proc-root/cwd and accidentally inheritable descriptor denial,
zero effective capabilities, denied namespace/mount/chroot operations and real
readonly Git/runtime open failures. Project reads/writes and private session
writes are positive controls. The original descriptor-table test and signed
socket/SQLite Allow/Deny test remain required alongside it.

The rehearsal also deliberately connects to fictional loopback and abstract
Unix listeners. The abstract listener transfers the fictional key descriptor
and the child reads it. This success demonstrates the exposure the deployment
service map must exclude; it is not a passed custody-denial check. A hidden
pathname socket remains unreachable. No model, Telegram, VM manager, real
credential or live service is called. Missing fixtures fail the strict job;
developer-mode skips are not namespace evidence.

## Installed acceptance after separate authorization

1. Recheck the exact running revision, state schema, leases, queue, outbox and
   unresolved outcomes using the installed schema-compatible read-only tools.
   Preserve unknown work; do not replay, release roots or move sessions to
   obtain a clean canary. Prepare a consistent backup and rollback artifact.
2. An administrator outside every productive domain prepares the VM, immutable
   artifacts and identities from the reviewed bundle. No privileged bootstrap
   credential is handed to a model process. Review actual hypervisor ACLs,
   virtual disk/snapshot permissions and guest privilege/forwarding policies
   from that independent management context.
3. From each real agent identity and its actual launch boundary, run bounded
   automated OS checks against fictional sentinels at the intended authority
   locations. Attempt reads, writes, proc aliases, ptrace, inherited/described
   descriptors, management access, nested namespaces and unintended endpoints.
   Privileged attacks must have a positive fixture control so a missing target
   cannot masquerade as denial. Never probe real keys or destructive controls.
4. Observe the entire trusted-UID process population and every launch path.
   Record installed executable hashes and namespaces, not merely absent PIDs
   in one snapshot. Any unconfined model-controlled process, undeclared launch
   or FD-sharing service blocks activation. Rehearse controlled launch/restart
   of each eligible path; reject paths without an enforced boundary. The offline
   corpus does not implement this inventory or prove its completeness.
5. Run the three strict scenarios using the exact installed kernel/bubblewrap
   and compatible fixtures. Then perform the separately authorized native
   Claude and real human callback cases in the file-tool runbook, including
   stop, restart and stale/duplicate decisions. Automate the actor where
   possible; do not ask the owner to perform repetitive terminal or button QA.
6. Confirm the actual CPA upstream subscription route and no paid fallback
   independently. Require exact clean revision convergence for every required
   long-running component and compatibility of independent consumers. Record
   OS custody, adapter evidence and native/Telegram acceptance as distinct
   results; no one result implies the others.

## Stop and rollback

Any uncertain action, failed check or unexpected agent privilege stops promotion.
Before first productive activation, discard only the staged activation decision;
keep the existing deployment and candidate artifacts untouched. Staging a second
environment must not start another Controller/poller or writer on the live state.

After a separately authorized cutover, stop new admission through the service
manager and preserve executing/indeterminate work and receipt history. Return to
the distinct schema-compatible rollback runtime using the existing release
manifest procedure; do not restore an older database over accepted work. Restore
text-only behavior only for new sessions, using the existing explicit session
mode transition. Never restart the old environment until the new pollers/writers
are stopped and ownership is independently checked. Keep both artifacts, keys
and recovery evidence private; revoke or rotate keys only in an explicitly
authorized credential task.

## Closure

Open. The launch/service inventory, provider-domain implementations, VM staging,
custody acceptance and native/Telegram checks remain pending. Next trigger is a
reviewed complete deployment bundle; this document does not request or grant
deployment authorization. A successful offline corpus closes only its named
synthetic tests.
