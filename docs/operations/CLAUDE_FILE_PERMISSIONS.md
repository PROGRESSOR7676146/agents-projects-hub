# Claude protected file-tool boundary

Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Choice and limitations: [ADR 0052](../decisions/0052-protected-claude-file-permissions.md).
This runbook prepares a candidate; it does not authorize deployment or provider calls.

Live activation remains blocked until untrusted processes are demonstrably
excluded from the worker/tlive keys, state and endpoints. A same-UID process
outside the Claude namespace can read mode-0600 keys and forge symmetric
receipts. Do not treat this slice as advisor isolation. Dedicated key custody,
separate OS identities and asymmetric result signing require a follow-up design
and acceptance before broadening this trust claim.

## Prepare an isolated candidate

1. Keep the existing text-only configuration as rollback behavior. Build an
   immutable Hub artifact containing the hook module. Use a compatible runtime
   rollback artifact supporting schema 38; a schema-37-only executable cannot
   roll back against migrated state. Use the ordinary release manifest/backup
   gates before any separately authorized migration or activation.
   Native sessions pin mode and a home digest. Use `/new` before enabling or
   disabling file tools or moving the session store; transcripts are not silently
   copied across homes. Configuration rollback restores behavior for new sessions.
2. Stage the exact tlive release using the
   [pinned patch procedure](../../integrations/tlive/README.md). Verify source
   hashes after patching and building; do not modify the running installation.
3. Install Python, Claude and the hook in trusted readonly runtime locations
   owned by another UID and unwritable by the provider UID/groups. The selected
   Python must resolve `hermes_codex_router.claude_permission_hook` with `-I -m`;
   an editable checkout or PYTHONPATH is not an installation. Configure only
   explicit runtime roots under `/usr` or a dedicated `/opt` prefix. Do not mount
   operator homes or generic configuration/data directories.
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
   sender must match. The explicitly named Hub transport file uses `version`,
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
hardlinks/special files in writable trees and nested mounts. Worktree roots
whose `.git` is a file are outside this initial slice. Missing isolation or
protected capability prevents productive invocation; it never selects a plain CLI.
Project and provider-session roots must use ext4, xfs, btrfs or tmpfs. DrvFs/9p,
NTFS, FUSE, vfat and unknown filesystem types are refused before invocation;
their aliasing and ownership semantics do not establish this boundary.

## Offline checks

Run focused Python coverage through the normal validator:

```sh
python scripts/validate.py --profile focused \
  tests.test_claude_permission_protocol tests.test_tlive_permissions \
  tests.test_claude_permission_hook tests.test_claude_permissions_journal \
  tests.test_claude_permission_host_roundtrip tests.test_claude_file_sandbox \
  tests.test_claude_permissions_config tests.test_claude_permissions_migration \
  tests.test_claude_file_policy tests.test_external_runtime \
  tests.test_tlive_extension
```

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

## Separate live acceptance

Before enabling this in a deployment, prepare the exact candidate/rollback,
temporary root/topic and expected effects for explicit owner authorization.
Verify native start/resume with the same session, Allow once and Deny, unreachable
host, timeout, duplicate/stale callbacks, stop while waiting, daemon/worker restart,
unknown card delivery and wrong root/generation/lease. Attempt private symlink,
Git-metadata/runtime modification and host-process access inside the namespace.
Confirm no Stop hook, continuation or second writer, and no real model calls
from monitoring. The worker process and the hook must have different visible
authority: keys/state are available only to the worker and tlive.

Confirm the actual CPA subscription route and absence of paid fallback separately.
The source patch/build checks do not pin downloaded dependencies or attest to
the built runtime. Release preparation must record dependency provenance and
the immutable artifact hash before a deployment can be accepted.
Name the exact clean revision reported by every required component. Store live
identities, receipts and transcripts outside Git. A provider invocation is not
approved merely by completing this runbook, and offline tests do not close this gate.
