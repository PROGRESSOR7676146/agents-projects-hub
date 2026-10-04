# Claude authority custody preparation

Status: existing-host assessment first; infrastructure and live acceptance pending.
Source baseline: `6a848c7a326b35e4c3ea552339e8c5c3d7beba53`.
Integration owner: Hub maintainer. Last verified baseline is source evidence;
candidate changes require their own publication checks.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Design: [ADR 0053](../decisions/0053-claude-custody-reference-deployment.md).
Keep the existing [file-tool runbook](CLAUDE_FILE_PERMISSIONS.md) for its
runtime, mount, key, tlive and native permission procedures.

## Assess the existing boundary first

The revised ADR keeps a VM as a reserve option. Start with the narrow implemented
boundary, then inspect actual launch and service exposure. Do not select a UID
layout, relax the live gate or migrate platforms merely to finish this plan.
This is a source assessment, not an inventory of any installed host:

| Launch path | Current source evidence | Remaining custody question |
| --- | --- | --- |
| Claude file-tool turn | `claude_file_sandbox.py` pins explicit mounts, hides host files/PIDs, drops capabilities and preserves only the per-turn permission socket. | Shared network exposure remains; installed descriptor closure and authority-service exclusion need evidence. Writable project/session mounts do not make this an advisor boundary. |
| Text-only Claude | `external_runtime.py` disables tools/customizations and runs the CLI without the file-tool namespace. | Trusted CLI/dependency and host execution authority need separate assessment; text-only flags do not prove OS custody. |
| Codex shared/stdio app-server | `codex_appserver.py` preserves native sandbox and approval policy; stdio launches the official app-server. | Neither route uses the Claude wrapper. Verify effective access to authority files, processes, endpoints and host management without weakening Codex policy. |
| Hermes native Gateway | `hermes_plugin.py` adds Hub admission and visible-context integration; Gateway owns native execution. | The plugin does not establish an OS boundary. Inspect the Gateway's actual tools, children, MCP and recovery paths independently. |
| OpenCode/Antigravity | `external_runtime.py` owns their CLI invocation and safe argument construction. | Provider sandbox flags are not evidence of Hub/tlive authority exclusion. Inspect actual execution identities and access. |
| Native/local transfer | `local_transfer.py` builds Codex remote attach and OpenCode/Antigravity resume commands; Claude transfer is unsupported. | Writer leases prevent competing Hub writers, not credential access. Local tools/hooks/children must retain the chosen custody boundary. |
| Helpers and outside assistants | No host-wide model-process launcher is implemented by this custody slice. | Inventory every actual helper, development and recovery launch; a read-only role or Plan mode does not prevent key reads. |
| Passive capability checks | `claude_cli_capabilities.py` runs CLI metadata without inference. | Verify executable provenance and launch authority; distinguish trusted metadata execution from model-controlled code. |

For each actual path, record the effective identity, enforced filesystem/PID
boundary, accessible local services and management capability. Use fictional
sentinels to test denied authority reads/writes and permitted project/session
access. Keep absent, unsupported and untested paths explicit. A process snapshot
or declared setting cannot prove future starts and resumes stay confined.

Choose the smallest candidate that can meet REQ-SEC-008 with those facts. Assess
separate Linux identities only under an independently trusted management boundary;
current hook/tlive peer checks are not compatible with arbitrary UID changes.
If smaller options cannot close a named exposure, document the reason and cost
before recommending the reserve VM. The architecture and remaining live gate
are owned by ADR 0053 and the product contract, not this source matrix.

## Prepare a reviewable deployment bundle

Prepare the following privately before requesting any live action. Do not put
inventory, credentials, receipts or administrative identities in the checkout.

| Artifact | Required content and stop condition |
| --- | --- |
| Management map | Every relevant host admin, backup/recovery access path and agent execution identity; include hypervisor/disk/snapshot/guest access only for a VM candidate. Stop if a model-controlled path can regain authority. |
| Launch map | Productive start/resume, text-only CLI, shared/stdio app-server, local transfer, direct-message, helper, hook, MCP, recovery and capability-probe paths. Mark each trusted-only, technically confined, or blocked; a missing path blocks cutover. |
| Service map | Reachable TCP listeners, pathname and abstract Unix sockets, system/user buses, forwarding and descriptor-passing services. Record intended agent exposure and authentication; unknown or unintended authority-bearing exposure blocks activation. |
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
2. An administrator outside every model-controlled domain prepares only the
   separately authorized topology, immutable artifacts and identities from the
   reviewed bundle. No privileged bootstrap credential is handed to a model
   process. Verify host administration, privilege, forwarding and recovery
   access independently; for a VM candidate also verify hypervisor ACLs and
   virtual disk/snapshot permissions. Preparing this bundle does not select a VM.
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

Open. The installed launch/service assessment, selection of the smallest
enforceable custody boundary, missing controls and native/Telegram acceptance
remain pending. Next trigger is a reviewed assessment of the existing host's
actual exposure, followed by bounded controls and a complete deployment bundle.
VM staging is conditional on a justified, separately authorized selection.
A successful offline corpus closes only its named synthetic tests.
