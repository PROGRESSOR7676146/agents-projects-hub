# Claude protected file-tool boundary

Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Choice and limitations: [ADR 0052](../decisions/0052-protected-claude-file-permissions.md).
This runbook prepares a candidate; it does not authorize deployment or provider calls.

Use the trusted personal-installation scope in
[ADR 0072](../decisions/0072-trusted-personal-installation.md). A same-UID/admin
process outside the Claude namespace can read mode-0600 keys and forge symmetric
receipts; exclusion of all such assistants is not claimed or a release gate.
The confined launch's private-path, immutable-runtime and human-permission checks
remain mandatory. The finite [installation checklist](CLAUDE_CUSTODY.md) replaces
the former global inventory/UID/VM study. No new infrastructure is required here.

## Bash decisions and card settlement

The protected response window and its cancellation boundaries are defined in
[REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md). Verify the entire chain:
journal expiry, protected tlive cap, worker transport, hook broker and native
hook timeout. Check the shared per-launch credit cap and background-execution
limits in the contract: repeated cards do not renew the total wait budget. A matching
worker and tlive patch are required; an older tlive safely refuses the extended
expiry. Use the same worker-before-tlive update order below, after both are idle.
Existing pending cards are not renewed or replayed during an update.

In the developer profile, `Allow once` and `Deny` retain their exact request
scope. `Bash until restart` explicitly permits later commands in that same
Claude native session/generation and project on the accepting worker. It is
cleared by restarting that Claude worker, or invalidated by restarting tlive;
other worker slots can require another approval. `/stop` stops work but does
not revoke this consent. No grant is persisted in native settings or loaded
from history. Full boundaries: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

The protected card begins its outcome edit immediately after validated
settlement: buttons disappear and the original preview remains with the decision.
This edit does not wait for the command or block its permission receipt.
Telegram failures can leave an old-looking card; repeated clicks still cannot
change an already consumed decision. Check callback and edit transport separately.
Update the worker before the matching tlive patch; reverse that order for
rollback. New tlive with an older worker can display consent that the worker
safely rejects. With both updated, accept the new grant,
run another command without another card, and verify new permission after each
relevant restart. Keep ordinary Allow/Deny, stop and another-session checks.
These are separately authorized live checks, not a claim from source tests.

## Prepare an isolated candidate

1. Keep the existing text-only configuration as rollback behavior. Build an
   immutable Hub artifact containing the hook module. Use a compatible runtime
   rollback artifact supporting the candidate's retained current schema; merely
   supporting the original schema 38 is insufficient after later migrations. Use the ordinary release manifest/backup
   gates before any separately authorized migration or activation.
   Native sessions pin mode and a home digest. Use `/new` before enabling or
   disabling file tools or moving the session store; transcripts are not silently
   copied across homes. Configuration rollback restores behavior for new sessions.
   Before switching the configured profile or storage, run the candidate's
   `agents-projects-hub doctor SHADOW_CONFIG` against current state with the
   intended worker environment. Its required `claude_session_modes` check
   detects incompatible active and satellite conversations without invoking a
   model or changing session pins. Stop the rollout on that failure: retain the
   previous configuration for existing conversations, or arrange explicitly
   chosen new conversations before the change. Never rewrite stored digests to
   bypass the mismatch or automatically replay a refused task.
2. Stage the exact tlive release using the
   [pinned patch procedure](../../integrations/tlive/README.md). Verify source
   hashes after patching and building; do not modify the running installation.
3. Install Python, Claude and the hook in trusted readonly runtime locations
   owned by root, including their ancestors and descendants, without group/other
   write or POSIX ACLs. Another non-root UID is not a trusted installer by itself.
   The selected
   Python must resolve `hermes_codex_router.claude_permission_hook` with `-I -m`;
   an editable checkout or PYTHONPATH is not an installation. Configure only
   explicit runtime roots under `/usr` or a dedicated `/opt` prefix. Do not mount
   operator homes or generic configuration/data directories. Bubblewrap must
   protect synthetic runtime ancestors and system aliases with a final,
   nonrecursive readonly root remount; explicit project, session home and temporary mounts keep
   their own access modes. For a narrow runtime, mount canonical `/usr/bin/dash`
   explicitly: a recognized host `/usr/bin/sh` alias is recreated only for that
   already-pinned target. This supplies the CLI hook-runtime shell; it does not
   expose a shell tool to the provider. Other shell layouts are not silently substituted.
   Bubblewrap must
   advertise `--bind-fd` and `--ro-bind-fd`; missing support refuses invocation.
   These options are present in upstream 0.10.0 and Ubuntu's 0.9.0 security
   backport; an unpatched upstream 0.9.0 lacks them. Check capabilities rather
   than the displayed version. Group-writable prefixes are refused even when
   the worker is not a member of that group. User-owned pyenv/uv environments
   also cannot supply this trusted runtime. Install a dedicated root-owned
   runtime, remove group/other write and POSIX ACLs before separately accepting it.
   Interpreters and dynamic dependencies of the resolved Claude executable
   must also lie within the readonly runtime roots; prefer a self-contained
   root-owned build.
4. Create a distinct private mode-0700 provider-session base outside project
   roots and Hub/tlive authority directories. Each native session gets its own
   subdirectory. Existing user configuration, hooks, plugins and credentials
   must not be copied into it. Keep the already authorized CPA client credential
   source in the worker environment; the namespace strips unrelated environment
   variables and host credential stores.
5. Prepare two distinct random 32-byte signing keys in private mode-0600 files.
   Never place their values in shell arguments, examples, logs, Git or Telegram.
   tlive's `protected-permissions.json` uses `version`, `ownerId`, `chatId`,
   `requestKey`, `resultKey`; its configured sole Telegram chat and allowlisted
   sender must match. Hub checks Linux kernel credentials of incoming hook
   clients and the outgoing tlive connection against the effective worker UID,
   before request bytes or state access. The tlive listener itself relies on
   private socket permissions and signed transport messages.
   This rejects another UID, not a compromised process sharing the trusted UID.
   The explicitly named Hub transport file uses `version`,
   `socket_path`, `owner_id`, `chat_id`, `request_key`, `result_key`. Match both
   keys and pin one positive owner private-chat identity. Do not install the
   standard tlive Claude plugin or its Stop/continuation hooks.

An optional Hub configuration object has this reusable shape:

```json
{
  "claude_file_permissions": {
    "tlive_config": "/home/example/.config/example-hub/protected-transport.json",
    "tlive_home": "/home/example/.local/share/example-tlive",
    "provider_home": "/home/example/provider-sessions",
    "runtime_roots": ["/opt/example-runtime"],
    "python_executable": "/opt/example-runtime/bin/python",
    "hook_code_root": "/opt/example-runtime/lib/python3.11/site-packages",
    "bwrap_executable": "/usr/bin/bwrap",
    "private_paths": ["/home/example/.config", "/home/example/.local/state/example-hub"]
  }
}
```

Paths are fictional and must be explicit canonical sources. The worker also
excludes Hub state, registry, bot token files, the transport key file, tlive home and tlive
socket. Review all additional private endpoints/authority roots. Namespace
validation refuses symlinks, overlapping private mounts, writable trusted code,
hardlinks/special files in writable trees and nested mounts. Multiply linked
regular runtime/hook files are also refused: readonly exposure is still a
confidentiality boundary. Runtime distributions containing hardlinks require
narrower compatible sources; immutability alone does not waive this check. Runtime and hook
roots must also be free of submounts; choose narrower sources when a system
runtime tree contains them. Nested btrfs subvolumes are refused by the device
consistency check. Standard linked worktrees use the confined selected Git mounts
of [ADR 0076](../decisions/0076-confined-linked-worktree-git.md); unrelated worktree
metadata and the primary checkout are absent. Shared config is read-only and
repository administration requiring common-directory writes remains unsupported.
Operational `.hub/work` is hidden; keep backups and review authority there or
outside the project, never elsewhere in the provider-visible checkout. Local clones with
hardlinked Git objects are refused too: `git clone /path/to/repo` hardlinks
`.git/objects` by default, so clone a local repository with `--no-hardlinks` or from
its remote URL. To repair an existing clone, replace each multiply linked file under
`.git/objects` with a copy of itself (copy to a temporary name in the same directory,
keep its mode, then rename it over the original) and rerun `git fsck`. The writable-tree
scan also refuses a tree with more than 100,000 entries, hidden `.hub/work` included;
keep dependency, build and tool caches such as uv caches or `__pycache__` trees
outside the project. Missing isolation or
protected capability prevents productive invocation; it never selects a plain CLI.

For the optional developer candidate, add `"profile": "developer"` to that
object and start a new native session with `/new`. Omitting the field preserves
editor behavior. Profile changes and rollback cannot resume a conversation
under another profile. The selected immutable runtime must also contain the
actual Bash executable, Git, test interpreter and their dependencies; missing
tools must not trigger a host-side execution fallback. Follow
[ADR 0075](../decisions/0075-claude-personal-developer-profile.md) for scope and
acceptance. This configuration example grants no installation or live acceptance.

For the `workstation` profile, set `"profile": "workstation"` and start with
`/new`; no other key is needed. The whole host is readonly inside the namespace,
including credentials, as for Codex. Writes reach the project, its Git metadata
and the session HOME at its host path; `/tmp` is disposable and the project
`.hub/work` is created when absent and kept readonly. Host `/run`
stays visible, so resolvers and Docker sockets work as for other agents; the
permission socket is at `/tmp/hub-permission.sock`, and
foreground Bash runs without a card. Nested user namespaces work, so the
commit gate runs inside it; modules that launch the real root-owned runtime
skip there and run in CI and on the host. The host root is shown, not trust-scanned,
so the filesystem checks below apply to the writable and runtime mounts only.
See [ADR 0081](../decisions/0081-claude-workstation-host-view.md).

All mounted sources must use ext4, xfs, btrfs or tmpfs. DrvFs/9p,
NTFS, FUSE, vfat and unknown filesystem types are refused before invocation;
their aliasing and ownership semantics do not establish this boundary.
The worker pins mount sources with no-follow descriptor walks before checking
them and scanning writable trees, identifies their mounts by kernel mount ID,
and passes only those descriptors into bubblewrap. Name/ancestor replacement
cannot substitute another source after pinning. The owned runner closes its
copies on every exit; bubblewrap consumes them before the native child starts.
This does not freeze directory contents or attest to custody against an
unconfined same-UID process, hostile administrator or concurrent host mount
changes. The separate live isolation gate remains mandatory.

Private authority exclusion also compares descriptor inode identities and
filesystem coordinates derived from each selected kernel mount ID, device,
mount root and relative source path. This excludes bind aliases at a source
root, an ancestor or a protected subtree, including readonly runtime/hook mounts.
Different sibling directories on one filesystem remain usable. Missing private
paths use a separately pinned existing ancestor and the exact missing suffix;
the missing component must still be absent at the final recheck. Validation-only
private descriptors are closed before returning the launch and are never passed
to bubblewrap. Rechecks include mount IDs and relevant provenance/topology.
The kernel descriptor path must match its configured source/anchor exactly;
casefold name divergence, deleted paths and unavailable descriptor paths refuse.
Mountinfo is a bounded read, not an atomic view under concurrent host mutation.
Both source-topology and private-provenance checks share its strict parser:
records use only LF and fields only ASCII space. Other Unicode/control whitespace
is preserved as filename data; kernel path escapes are decoded once. Invalid
UTF-8, empty interior records/tokens and unsupported tail shapes refuse invocation.
The initial boundary refuses private authority paths containing descendant
mounts rather than omit their separate filesystems. Unavailable, ambiguous,
malformed or mismatching mount provenance also refuses invocation. These checks
do not freeze contents or remove the scoped installed/native/live gates.
The fresh per-turn socket is also checked against private authority; it cannot
substitute a protected transport endpoint. Strict provenance parsing can refuse
unusual unrelated kernel mount records; such layouts require offline investigation,
never an invocation outside the wrapper.
The host creates the endpoint in a fresh system temporary directory. That directory
must be outside configured private authority paths; an overlapping temporary
directory is refused rather than exempted from exclusion.

The trusted Claude worker parent must retain the host UID namespace. In a user
systemd manager, `PrivateTmp` or a `BindPaths` mount may implicitly enable a user
namespace that maps only the service UID. Root-owned runtime files then appear
owned by the overflow UID and correctly fail immutable-runtime validation. The
primary Claude instance drop-in disables `PrivateTmp`/`PrivateUsers` and clears
the generic template's Codex-only `BindPaths`; the dedicated Claude slot template
uses the same host-UID policy. Bootstrap installs both. For an existing immutable
deployment, stage and inspect the merged unit as a separately authorized service
change, then restart only an idle Claude worker. Preserve `NoNewPrivileges`,
private umask, runtime ownership checks and the model's bubblewrap boundary;
never accept the overflow UID as trusted. Other providers retain their unit policy.

## Offline checks

Run focused Python coverage through the normal validator:

```sh
python scripts/validate.py --profile focused \
  tests.test_claude_permission_protocol tests.test_tlive_permissions \
  tests.test_claude_permission_hook tests.test_claude_permissions_journal \
  tests.test_claude_permission_host_roundtrip tests.test_claude_file_sandbox \
  tests.test_claude_permissions_config tests.test_claude_permissions_migration \
  tests.test_claude_file_policy tests.test_external_runtime \
  tests.test_tlive_extension tests.test_claude_mount_pins tests.test_unix_peer
```

Also select `tests.test_claude_private_mount_aliases` and
`tests.test_claude_private_mount_provenance` after mount-boundary changes. Their
scripted metadata fixtures reproduce alias, missing-path, replacement and
descriptor-cleanup failures; the real ephemeral namespace fixture checks root,
ancestor and subtree bind aliases using fictional data without a provider.
Readonly runtime/home aliases and missing-prefix variations additionally have
scripted metadata coverage; that is not a real-kernel invocation claim.
`HUB_REQUIRE_NAMESPACE_TESTS=1` makes missing namespace support fail instead of
skip, as in the required CI namespace profile. These are prelaunch source
checks, not productive native/Telegram acceptance or whole-host custody.

Select `tests.test_namespace_worktrees` for linked roots. Its real namespace
witness performs an edit/add/commit using temporary Git worktrees without a
provider or Telegram call, and checks primary/sibling and operational exclusions.

The roundtrip tests require a local environment that permits Unix-socket bind.
They use temporary Git roots, SQLite and signed fictional Telegram receipts.
For the verified patched source, the separate tlive test uses real IPC and an
authenticated local dashboard, with Telegram and app-server custody mocked:

```sh
TLIVE_SOURCE=/tmp/example-tlive/package node --experimental-transform-types \
  --loader ./integrations/tlive/ipc-test-loader.mjs \
  ./integrations/tlive/ipc-adversarial.mjs
```

This proves the protected namespace and old-channel exclusion, not actual human
Telegram delivery. Canonical validation, independent exact-candidate review and
hosted gates still apply before publication.

The opt-in [native editor fixture](../testing/CLAUDE_NATIVE_EDITOR.md)
checks the pinned CLI actually invokes PermissionRequest before Write/Edit,
preserves bytes on Deny and requires a new decision for the same file after
exact resume. Direct hook roundtrips alone do not prove that native behavior.
Fictional receipts and readonly test mounts do not attest to installed custody
or real human approval; keep those live gates open.

## Unsupported native transfer recovery

Claude `/local` and `/return` are refused before transfer preparation, writer
mutation or summary enqueue, including for configured aliases of the Claude
runtime. The command receipt still deduplicates an exact Telegram redelivery.
If retained state has a local Claude writer, preserve that lease, native identity
and earlier held work for explicit local investigation. The refusal does not
close the CLI or establish a safe handover; do not clear the lease or use a
generic non-Codex summary as transfer proof. Supported native transfer and saved
session discovery remain separate parity work under
[REQ-WRITER-006..007](../product/ACCOUNTS_CONTROL_AND_SECURITY.md#implemented-minimal-native-transfer).

`tests.test_claude_transfer_refusal` covers retained local leases, aliases,
missing native identity, duplicate controls and unchanged held work, using only
fake transports and disposable state. Other supported runtimes keep their
existing transfer paths.

## Separate live acceptance

Before enabling this in a deployment, prepare the exact candidate/rollback,
temporary root/topic and expected effects for explicit owner authorization.
Verify native start/resume with the same session, Allow once and Deny, unreachable
host, timeout, duplicate/stale callbacks, stop while waiting, daemon/worker restart,
unknown card delivery and wrong root/generation/lease. Attempt private symlink,
Git-metadata/runtime modification and host-process access inside the namespace.
On the exact installed bubblewrap build, inspect accessible descriptors of every
process visible in the namespace, including PID 1; no pinned host-source inode
may remain accessible there. Record explicitly whether each process's fd table
was readable or inaccessible. The executable's version or help flags alone do
not establish this descriptor-closure property.
Check separately that a project, provider home or runtime bind alias of a private authority
root, ancestor or subtree refuses before provider invocation, while an unrelated
same-filesystem project still starts. Private descendant mounts must refuse;
missing authority prefixes that appear or change during preparation must refuse.
Confirm no Stop hook, continuation or second writer, and no real model calls
from monitoring. The worker process and the hook must have different visible
authority inside that launch: keys/state must be absent from the hook/provider
namespace and available to the worker/tlive trust base. This is no claim about
unconfined trusted administrative processes outside it.

Confirm the actual CPA subscription route and absence of paid fallback separately.
The source patch/build checks do not pin downloaded dependencies or attest to
the built runtime. Release preparation must record dependency provenance and
the immutable artifact hash before a deployment can be accepted.
Name the exact clean revision reported by every required component. Store live
identities, receipts and transcripts outside Git. A provider invocation is not
approved merely by completing this runbook, and offline tests do not close this gate.
