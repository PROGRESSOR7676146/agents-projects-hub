# Prepared release switching

Status: source candidate, tested only with temporary releases and owned fake
services. Contracts: [REQ-OPS-010/011/015](../product/PERSISTENCE_AND_RECOVERY.md).
Rationale: [ADR 0089](../decisions/0089-prepared-release-switcher.md).

`scripts/stack.py` uses only system Python's standard library on Linux. Pin a
reviewed copy outside the releases it will manage; do not run an updater from
the release it replaces. The tool neither downloads nor installs packages.
Prepare exact clean versions with the owning build/package tools, verify their
dependencies and artifacts, then seal regular files and directories read-only
before planning. External symlinks may target only immutable OS code under
`/usr`; release-internal symlinks must not escape. Hardlinks and special files
are refused. Never modify or delete a running or retained rollback release.

## Manifest

Keep the manifest outside Git as a current-user-owned mode-0600 file. All paths
are explicit and canonical, without symlinked ancestors. The state directory
is separate, mode 0700. Every component needs an existing `current` symlink
into its own version directory. Initial migration from in-place installations
is a separately authorized deployment step. The following names and paths are
fictional:

```json
{
  "schema_version": 1,
  "state_dir": "/home/example/.local/state/agents-projects-hub/stack",
  "components": [
    {
      "id": "proxy",
      "releases": "/home/example/.local/share/example-proxy/releases",
      "link": "/home/example/.local/share/example-proxy/current",
      "target": "reviewed-version",
      "group": "provider-core"
    }
  ],
  "groups": [
    {
      "id": "provider-core",
      "units": ["example-proxy.service", "example-consumer.service"],
      "preflight": [["/home/example/release-checks", "idle-and-backup"]],
      "check": [["/home/example/release-checks", "compatible", "{proxy_before}", "{proxy_after}"]],
      "health": [["/home/example/release-checks", "exact-health", "{proxy}"]]
    }
  ]
}
```

Include exactly the components and dependent consumers in the assigned window.
Put components sharing a service, such as a coupled client/server pair, into
one group. A service may occur in only one group. Groups are ordered by
dependency; units inside a group are ordered producer before consumer. The
tool stops in reverse unit order, switches the group's links while stopped,
then starts in forward order. A failure rolls groups back in reverse order.

Gate commands are required nonempty argv arrays, without shell interpolation.
Each command has a 120-second timeout; output is suppressed and errors reveal
no command arguments. They must not invoke inference, change versions or
replay work. `{proxy}` denotes the selected candidate during forward checks
and the old release during rollback; `_before` and `_after` always name the
recorded pair. The example checks executable does not ship with this tool:
each real integration must supply and test its owning checks.

- `preflight`: prove idle/settled work, authorization prerequisites and a fresh
  verified consistent backup. It runs again immediately before a forward
  switch and before rollback. A failed native-custody/drain guard must refuse.
- `check`: verify exact artifacts, configuration and both runtimes' support
  for the retained schema. Use the existing [Hub manifest and dry-run
  gates](IMMUTABLE_RELEASES.md) for Hub; rollback must recheck against current
  state, not an old backup. This tool performs no migration.
- `health`: verify every affected service's exact selected revision and passive
  readiness. Process liveness alone is insufficient. Live inference/Telegram
  acceptance is a separately authorized step outside this tool.

## Commands

```bash
python3 /home/example/tools/stack.py /home/example/stack-manifest.json plan
python3 /home/example/tools/stack.py /home/example/stack-manifest.json apply --confirm PLAN_DIGEST
python3 /home/example/tools/stack.py /home/example/stack-manifest.json rollback --confirm PLAN_DIGEST
```

`plan` is read-only, executes no checks or services and writes no files. Its
digest binds the full manifest, current links, both release trees and the
previous switch record. Apply checks the digest under an exclusive lock,
checks candidate compatibility, rechecks the plan, and saves `switch.json`
before the first service action. Successful repetition requires a fresh plan;
an unfinished switch blocks another apply. A second concurrent operation
refuses immediately. Rollback binds the recorded plan and is idempotent.

A unit may use the pinned script's `launch MANIFEST_COMPONENT RELATIVE_EXECUTABLE`
after the manifest argument. For example:

```bash
python3 /home/example/tools/stack.py /home/example/stack-manifest.json launch proxy bin/proxy --config /home/example/proxy.json
```

The link is resolved once; the executable and `{dir}` argument placeholders
use that canonical release. Python bytecode writes are disabled for descendants.
Do not refer to `current` again in runtime imports or component configuration.

## Failure and recovery

Exit 0 means the requested operation passed; 1 means a service/check operation
failed (inspect the reported status); 2 means invalid/stale input or refusal.
One mode-0600 record holds the latest full plan and status. It is atomically
replaced and fsynced, as are link replacements. No database is opened by the
tool. Gate executables remain responsible for their declared data access.

On ordinary forward failure the tool attempts rollback and reports
`rolled_back` or `recovery_failed`. After process/host loss or interruption,
inspect the record and invoke explicit rollback with its plan digest. Recovery
accepts only that record's before/after links; even a missing candidate can be
replaced if the old release remains valid. A failed rollback retains the record
and can be retried. Changed old bytes, incompatible state, unrelated link drift
or corrupt evidence refuse recovery. Repair the cause through the owning
procedure; do not restore an old database over accepted work or erase the
record to force a new apply.

## Preparing a user-owned trusted runtime

This source candidate supports the explicitly configured alternative in
[REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md). Build a dedicated,
non-editable runtime in its final canonical version directory directly under
`releases`. Copy the interpreter (for example, create a venv with `--copies`),
install non-editably from the hash-locked dependencies and reviewed wheel, and
avoid hardlinks to package caches. Python must import the installed hook with
`-I -m`; do not reuse a mutable pyenv/uv cache or point configuration through `current`. Keep the
runtime, hook and their dependencies within explicit read-only mounts.
The already-installed root-owned bubblewrap remains a prerequisite.

Using the prepared version's Python, seal only that new version, then independently
verify the resulting digest. These example commands do not install or activate it:

```bash
/home/example/runtime/releases/version-one/bin/python -I -B -m hermes_codex_router.runtime_release seal /home/example/runtime/releases/version-one
/home/example/runtime/releases/version-one/bin/python -I -B -m hermes_codex_router.runtime_release verify /home/example/runtime/releases/version-one --sha256 EXACT_TREE_SHA256
```

`seal` inspects ownership, ancestors, ACLs, links and file types before removing
write bits; it follows no links when changing permissions. `pin` calculates a
digest for an already sealed version without changing permissions. All actions
return JSON and refuse unsafe versions. No service, configuration or state is
changed. Do not run `seal` on a running or retained rollback version.

Add `user_releases` to the **private** `claude_file_permissions` configuration,
beside the existing explicit runtime/executable/hook paths:

```json
{
  "user_releases": [
    {
      "root": "/home/example/runtime/releases/version-one",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ]
}
```

The digest above is fictional. Use the verified digest of the actual sealed
version. Omitting the field retains root-owned validation. The namespace checks
every registered tree before launch and refuses changed bytes or permissions.
The switcher uses the same digest format, but it does not write these pins or
prepare a release-specific configuration. Keep candidate and rollback paths/pins
with their own compatible private configurations; a future Hub adapter must
coordinate their selection. No active session is repinned by this tool.

## Integration still required

This script grants no model, Telegram or Hermes approval authority. Run a real
window only from its authorized independent executor, outside restarted units.
Expiring single-use owner controls, acquisition, drift detection and a Hermes
self-update watchdog are not implemented. `hermes` as a component is refused;
do not conceal that dependency under another ID.

The source policy now permits explicitly pinned user-owned Hub/Claude versions.
The current installation is not wired to this script. Release-specific private
configuration, Hub gates and unit launch wiring remain deployment integration;
source tests do not prove a live rollout, reboot, schema migration or an installed
no-sudo Hub update.
