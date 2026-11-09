# One-command Hub deployment: first source slice

The standalone tool now implements planning, a journaled deployment window,
status and recovery. Its evidence is limited to offline fixtures; independent
source review and installed acceptance are still required before operational use.
No live deployment or system service was exercised by these tests.

The source now validates the following version 1 input. All values below are
fictional; paths must be absolute, canonical and already exist. The ops
directory must be private, and both ops and releases directories must be owned
by the invoking user. The input itself must be owned by that user, mode 0600,
and outside a Git checkout. The last restart unit must be the controller.

```json
{
  "schema_version": 1,
  "units_dir": "/home/example/.config/systemd/user",
  "hub_config": "/home/example/.config/example/hub.json",
  "claude_config_dir": "/home/example/.config/example",
  "claude_config_pattern": "claude-*.json",
  "managed_dropin_name": "override.conf",
  "codex_manifest": "/home/example/.config/example/manifest.json",
  "state_db": "/home/example/.local/share/example/state.db",
  "releases_dir": "/home/example/.local/share/example/releases",
  "ops_dir": "/home/example/.local/share/example/ops",
  "native_claude": "/home/example/.cache/example/claude",
  "native_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "restart_order": ["example-worker.service", "example-controller.service"],
  "config_only_units": ["example-codex.service", "example-socket-directory.service"],
  "controller_unit": "example-controller.service",
  "untouched_units": ["example-codex.service", "example-tlive.service"],
  "monitor_service": "example-monitor.service",
  "monitor_timer": "example-monitor.timer",
  "github_repo": "example/example"
}
```

`untouched_units` lists the managed Codex app-server first, followed by tlive.
The first unit may initially be active or failed; the remaining units must be
active. The tool records their identities and never starts or stops them. It
compares the Codex socket only when the app-server was initially active. The
reference window also requires the fallback app-server to be inactive and checks
the effective managed app-server start timeout (five minutes) and socket-directory
start limit (zero); releases with different effective settings are refused.

`config_only_units` may be empty and may include untouched units, but must be
disjoint from the restart order and monitor units. Their managed runtime drop-ins
are planned, rewritten and restored on rollback; the runner never operates on
these services. After each reload it verifies their effective runtime references
and unchanged service identities. Runtime-bearing drop-ins must belong to the
restart order, config-only units or monitor service; other protected services
cannot share them. For a shared template drop-in, every active instance must
be in the restart order; the check is repeated around service effects. Inactive
or future instances pick up the rewritten configuration when started. Base
unit templates outside the deployment scope and shared templates are guards;
protected app-server and socket-directory template changes are refused. The
candidate may not change a unit's dependency configuration. Before service
effects and again after each reload, the tool reads effective dependency and
reverse-dependency properties through the service manager and follows start and
stop propagation, including indirect dependencies. It refuses a graph that can
operate on an untouched or config-only unit (including through an alias) or cannot be read completely. One exception: a config-only `Type=oneshot` unit without `RemainAfterExit`, whose identity is disjoint from every untouched unit, may be started through `Wants=`, `Requires=` or `BindsTo=`, because every dependent start re-runs such a preparation helper by design; stop propagation and `Upholds=` still refuse. Because each dependent start re-runs such a helper, convergence checks it by outcome (not `failed`, `Result=success`) rather than by unchanged service identity. Unit names are read after `--` and unquoted with shell rules, since systemd quotes escaped names. Ordering-only
`After=` and `Before=` do not propagate service operations.

For a prepared private plan, `apply --plan PLAN_DIRECTORY` rechecks the plan
under an exclusive lock. `deploy --release TAG` prepares, plans and applies in
one command. Both require `--installation` and refuse an unfinished window.
The window checks for busy jobs and schema compatibility, stops the timer and
monitor then the configured services in reverse order, rechecks for busy jobs,
backs up SQLite with the old runtime, creates and verifies a release manifest,
installs files atomically, reloads systemd and starts services in the configured
order. It retries the monitor oneshot once after 60 seconds and checks new
runtime convergence and untouched identities. Before and after effect records
are synced to the private journal.

The result and exit code are:

- `ACCEPTED` (0): core convergence and monitor check succeeded.
- `ROLLED_BACK` (2): the schema-compatible rollback runtime converged after recovery.
- `CORE_HEALTHY_MONITOR_INCOMPLETE` (3): core converged but both monitor checks failed; the new files remain installed.
- `RECOVERY_REQUIRED` (4): recovery could not be verified; preserve the window evidence and stop further changes.
- Refusal (1): the window was not accepted for execution.

On an interrupted window, `apply` and `deploy` print the complete
`rollback --window ID` command including the installation input. `status` shows
the last recorded window, live schema and currently applicable rollback target
and revision; recovery makes its final selection after stopping services.
Recovery accepts only the newest window and validates the complete saved file
inventory before effects and again after quiescence. With the original schema it
restores the previous runtime; with the target schema it installs the prepared
rollback release. Another schema stops recovery and requires an owner decision.
The selected artifact, runtime identity and pin are rechecked, and the choice is
journaled. Neither automatic nor explicit rollback restores a database backup.
Window records and backups remain in the private ops directory.

```text
python3.12 -I scripts/hub_deploy.py deploy --installation /home/example/private/installation.json --release v1.2.3
python3.12 -I scripts/hub_deploy.py apply --installation /home/example/private/installation.json --plan /home/example/private/ops/plan-example
python3.12 -I scripts/hub_deploy.py status --installation /home/example/private/installation.json
```

The `plan` command discovers the common managed runtime and Claude pin, checks
unit activity and residency, downloads and verifies both release asset sets,
compares their `schema_max` values with the read-only database schema, and builds
or verifies an inactive sealed candidate. A forward migration also requires
`--rollback-release TAG`: this distinct verified release must have the same
`schema_max` as the candidate and is prepared and validated in the same way.
A candidate below the live schema, or an old release whose schema differs from
live state, is refused. The plan binds both schemas and the rollback identity.
The backup is completed before the first new-runtime service starts; workers
may migrate the database before the controller starts. The live database must
still have the old schema before the backup and the target schema after start. It requires network access and writes
the candidate runtime and private evidence; it does not change installed service
files or restart services. A Claude config filename containing the first seven
characters of the old revision is changed to the new prefix; otherwise its
existing name is retained. The candidate runtime validates both Hub configs.
Release unit templates are included as changes only when their bytes differ;
unchanged templates are recorded as guards.

The private plan directory records before, after and applicable rollback copies and hashes, runtime
identities, service order and a digest of canonical plan JSON. The command
rechecks the digest, copies and installed files before reporting the plan. Old
release tags are resolved from the discovered revision; missing or ambiguous
release evidence is refused. The installed runtime's `release-info` does not
supply `schema_max`, so the gate uses verified published metadata for both
releases. Only offline fixture execution has been tested; no installed planning,
deployment or rollback has been accepted.

```text
python3.12 -I scripts/hub_deploy.py plan --installation /home/example/private/installation.json --release v1.2.3 --rollback-release v1.2.2
```
