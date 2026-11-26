# One-command Hub deployment: first source slice

The standalone tool now implements planning, a journaled deployment window,
status and recovery. Installed use, 2026-11-09: a forward schema migration
(55 → 56) exposed four service-manager gaps, fixed in source; recovery then
closed with `ROLLED_BACK`, and the next same-schema window was `ACCEPTED` with
every component converged on the release. The fixture tests remain the
regression evidence for each path.

Do not use standalone `apply` or `deploy` to execute a live restart: their
service stops bypass `MaintenanceWindow`, and their idle checks do not close
admission. Their journaled rollback does not replace the runbook's restoration.
Live restarts need a separately reviewed executor and an independent outcome
observer as required by the [Hub restart procedure](HUB_RESTART.ru.md).

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

`untouched_units` lists the managed Codex app-server first, followed by tlive
unless the installation switched tlive off ([below](#switching-tlive-off)).
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
operate on an untouched or config-only unit (including through an alias) or cannot be read completely. One exception: a config-only `Type=oneshot` unit without `RemainAfterExit`, whose identity is disjoint from every untouched unit, may be started through `Wants=`, `Requires=` or `BindsTo=`, because every dependent start re-runs such a preparation helper by design; stop propagation and `Upholds=` still refuse. Because each dependent start re-runs such a helper, checks before service starts (including recovery) cover only its configuration; after starts, convergence waits within its bound until it has finished (`inactive`/`dead`, `Result=success`) instead of requiring an unchanged service identity. Unit names are read after `--` and unquoted with shell rules, since systemd quotes escaped names. Ordering-only
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

A process-mode unit is resident when the main process of one identity snapshot
(`ActiveState`, `MainPID`, `InvocationID`) has an argument under the runtime
root and the snapshot is unchanged after the probe. A sandboxed `Type=simple`
unit is active before it executes its runtime, so a check made right after a
start, such as an executor's readiness hook, re-probes up to 150 times, with
15 seconds of sleep in total plus probe time, while that snapshot holds. It
refuses a restart or a state other than `active` at once and returns the
snapshot, which an executor compares with the invocation it started. Preflight
checks once, so its busy and load checks do not go stale; convergence repeats
its whole check every five seconds instead of waiting inside it.

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
inventory before effects and again after quiescence. A file matching none of its
recorded digests, such as a Hub configuration edited after the window, refuses
recovery as an unrelated file edit until its recorded bytes are restored. With the original schema it
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
A same-schema plan always rolls back to the installed previous release:
`--rollback-release` is refused for it, and `apply` refuses a saved same-schema
plan with separate rollback fields. From schema 59 the candidate must permit
artifact-loss declarations. A migration's rollback release must refuse them; a
same-schema previous release keeps its own verified capability, which the plan
binds and `apply` rechecks before any service stops. The candidate's own code
creates the in-window release manifest. A candidate released before this rule
still requires a refusing rollback, so over a permitting previous release its
window fails after services stop, before any file change or new-runtime start,
and recovery restarts the previous release.
The backup is completed before the first new-runtime service starts; workers
may migrate the database before the controller starts. The live database must
still have the old schema before the backup and the target schema after start. It requires network access and writes
the candidate runtime and private evidence; it does not change installed service
files or restart services. A Claude config filename containing the first seven
characters of the old revision is changed to the new prefix; otherwise its
existing name is retained. The candidate runtime validates both Hub configs.
The Hub config is the authority for Claude settings: the plan is refused when
its Claude agent definitions (except `executable`) or Claude permission
settings differ from the candidate Claude worker config. Only the release
overlay may differ — the CLI executable and a confined profile's runtime paths
and pin. The Controller and monitor read the Hub config, so a drifted copy there
would misreport the Claude workers' real configuration.
Release unit templates are included as changes only when their bytes differ;
unchanged templates are recorded as guards.

The private plan directory records before, after and applicable rollback copies and hashes, runtime
identities, service order and a digest of canonical plan JSON. The command
rechecks the digest, copies and installed files before reporting the plan. Old
release tags are resolved from the discovered revision; missing or ambiguous
release evidence is refused. The installed runtime's `release-info` does not
supply `schema_max`, so the gate uses verified published metadata for both
releases. Installed planning, a same-schema deployment and an explicit rollback
have been exercised; a forward migration has not yet completed as `ACCEPTED`.

```text
python3.12 -I scripts/hub_deploy.py plan --installation /home/example/private/installation.json --release v1.2.3 --rollback-release v1.2.2
```

## Hub config amendments

Never edit the installed Hub config by hand: the window's manifest binds its
bytes, so an edit breaks an external backup consumer's pin and makes release
recovery refuse it as an unrelated file edit. `reconfigure` instead records the
change as a journaled amendment of the newest window, which must be `ACCEPTED`
([ADR 0094](../decisions/0094-one-command-hub-deploy.md#amendment-hub-config-amendments-on-an-accepted-window)).
The plan, its digest and the window manifest stay untouched.

Write the complete new config to a private file (owned by the invoking user,
mode `0600`, absolute path) and pass it with `--config`. The tool compares
top-level keys with exact JSON values and refuses an unchanged config,
duplicate keys, non-JSON numbers and any changed key outside
`RECONFIGURABLE_KEYS`: `agent_mail`, `message_batch_max_ms`,
`message_batch_quiet_ms`, `session_handoff`, `task_no_progress_seconds`,
`tlive_enabled` and `topic_panel`. Workers read agent mail routes and the
handoff setting from the database, where the sender projects them at start
([ADR 0112](../decisions/0112-agent-mail-between-topics.md),
[ADR 0114](../decisions/0114-one-tap-new-session-with-handoff.md)). Numbers compare as exact
decimals, and `1`, `1.0` and `true` differ. Only the Controller, the sender
and the monitor, which reads the Hub config on every run, read these keys. Other keys stay outside `reconfigure`: Claude
workers read some from their own copy, a destination such as the alert topic
is the owner's choice, and the rest are trust, credentials, project
registration, security profiles, paths, worker counts or the unit set. The
installed runtime must validate the candidate. Both bound wheels must still
match, and the window backup must match the window manifest, which must in turn
match the digest the window journaled as `manifest_bound`: that of the bytes it
read before and after verifying the manifest.
A window from before that record has none, and its manifest is used as found.
Every other window
file must keep the bytes and mode the window installed. The installation must
pass the deployment preflight: no busy jobs, low load, every service resident
in the installed runtime, config-only units on that runtime and unchanged
untouched units.

The amendment stops the monitor and the restart order, rechecks for busy jobs,
writes the config atomically with its installed mode, creates a new
`release-manifest.json` in its own directory with the window's wheels and state
backup, verifies it with `--state` against the live database, requires it to
bind the same wheels, schema and backup as the window manifest, starts the
services and checks the same convergence as a window. The reported manifest
digest is that of the checked bytes, which must be unchanged on disk before the
amendment is accepted; its journal records it. Any failure restores the
previous bytes, restarts and verifies the services, and ends `REVERTED` only
after a fresh monitor run succeeds; otherwise it ends `RECOVERY_REQUIRED`. An
interrupted or unverified amendment blocks `deploy`, `apply` and further
amendments until `reconfigure --recover` restores its previous bytes. Recovery
refuses while provider jobs are busy, because the services may be running
again. The result names the changed keys, the new config digest and the new
manifest path and digest; report them for an external backup consumer to
re-pin. `status` lists the amendments and the manifest that binds the
installed config; when the installed bytes differ from the recorded ones, it
reports `config_drift` instead of a manifest.

A config already edited outside the tool is recorded with `--adopt`: the live
bytes become the amendment, checked against the same key list, and the
service cycle runs without a write. A failure restores the last recorded
bytes, not the hand edit.

A release rollback restores the config from before the window, dropping every
amendment that was not reverted. It refuses until the caller acknowledges this
with `--drop-amendments`, and accepts only the recorded installed bytes or
those of an unfinished amendment. The window journal records the
acknowledgement, a `ROLLED_BACK` result lists the dropped amendments, and the
flag is refused when there is nothing to drop, including on a window that was
already rolled back. Reapply a dropped change with a new `reconfigure` after
the next accepted window.

An explicit recovery, `rollback --window` or `reconfigure --recover`, may
follow a reboot that gave the untouched units new invocations. It therefore
takes their identities when it starts, observes the Codex socket with the
runtime it is about to start after restoring that runtime's files, journals
both as `recovery_baseline` and requires only that it leaves them unchanged. The automatic recovery inside a failed window or amendment
keeps the baseline taken by its own preflight.

```text
python3.12 -I scripts/hub_deploy.py reconfigure --installation /home/example/private/installation.json --config /home/example/private/hub-candidate.json
python3.12 -I scripts/hub_deploy.py reconfigure --installation /home/example/private/installation.json --adopt
python3.12 -I scripts/hub_deploy.py reconfigure --installation /home/example/private/installation.json --recover
python3.12 -I scripts/hub_deploy.py rollback --installation /home/example/private/installation.json --window window-00000000000000000001-example --drop-amendments
```

The result codes are those of a window, with `REVERTED` (2) for a restored
amendment. The tool runs from the repository checkout, not from the release
wheel, so it needs no Hub release. It is covered by offline fixture tests,
not yet by installed use.

## Switching tlive off

[ADR 0116](../decisions/0116-local-no-tlive-mode.md) lets an installation in
which no Hub work can ask a human through tlive switch it off. The deploy
preflight requires every untouched unit after the app-server to be active, and
`rollback --window` and `reconfigure` refuse a descriptor that differs from
the copy their window recorded. So the descriptor changes before a window,
never after it:

1. Tell an external backup consumer about the new service set first; it
   cannot learn that from its own check.
2. Keep the exact descriptor bytes, remove tlive's unit from
   `untouched_units`, then deploy the window of a release that knows
   `tlive_enabled`. That window records the new descriptor; tlive keeps
   running meanwhile. Until the window is accepted, recovery of the previous
   window needs the old descriptor: if the new window is not accepted,
   establish which window is current before restoring the matching bytes.
3. `reconfigure` with `"tlive_enabled": false` added. A runtime that knows the
   key refuses it unless Codex uses `full` and no confined Claude profile is
   configured; an older runtime accepts and ignores it, and the tool cannot
   tell the difference.
4. Before touching the service, run the doctor: it must report `tlive` as
   switched off by owner configuration and no `recovery:tlive` check. Without
   that positive result the installed runtime does not apply the switch; stop
   and revert the amendment instead.
5. Stop and disable only tlive's unit through the service manager. Keep its
   package, unit files, build and bot token.
6. Check passively: `recovery:hermes` is healthy, the next monitor run raises
   no tlive alert, and the consumer's check result goes into the report. Do not
   start a model turn for this; the next owner-initiated Codex turn completes
   the check, and the report lists it as pending until then.

The way back enables and starts tlive and checks that it is active before a
`reconfigure` sets `"tlive_enabled": true`; the key stays. Until a later
window puts tlive back into `untouched_units`, the deploy preflight does not
check it. Before any release rollback, look at the configuration and runtime
it restores: a release without the key, or a config from before the switch,
expects tlive again, so enable, start and check tlive first.

## Claude CLI store

With [ADR 0113](../decisions/0113-claude-cli-store-and-nightly-promotion.md),
an installation for the `host` Claude profile replaces `native_claude` and
`native_sha256` by two fields:

```json
{
  "claude_store": "/home/example/.local/share/example/claude-cli",
  "claude_window": "02:30-05:30"
}
```

The store is a dedicated user-owned directory without group or other write
access: empty at first use, then marked by `store.json`. `claude_window` is
local time and may cross midnight. Releases deployed in this
mode embed no Claude binary. Their worker configuration runs
`<claude_store>/current/claude`. The first such deploy copies the binary of the
active release into the store, runs the new release's gate for it and points
`current` at it. Every later deploy runs the new release's gate for the active
CLI, again right before the switch, and refuses the deploy when it fails.
`rollback --window` to a store release runs that release's gate for the active
CLI when it has no evidence and refuses when it fails.

```text
python3.12 -I scripts/hub_deploy.py claude-status --installation /home/example/private/installation.json
python3.12 -I scripts/hub_deploy.py claude-promote --installation /home/example/private/installation.json --candidate /home/example/.local/share/claude/versions/1.2.3 --candidate-version 1.2.3
python3.12 -I scripts/hub_deploy.py claude-rollback --installation /home/example/private/installation.json
```

The host update tool calls `claude-promote` in its nightly window, after it has
installed the host CLI, with the installed file as the candidate. Hub hashes a
copy and admits it only when its digest and size match the publisher's
manifest for that version. It then promotes the newest admitted candidate that
is at least one day old (23 hours on Hub's admission clock) and passes the
active release's gate, checks the switch and restores the previous selection on
failure. Outside the window `claude-promote` refuses; there is no bypass.
`claude-rollback` runs at any time, walks back one selection per call and
quarantines the build it leaves, so the next night does not promote it again.

Each run prints one JSON object with `outcome` and saves it under
`<claude_store>/results/`. Exit codes: 0 `promoted`, `rolled_back`, `current`
or `skipped`; 1 `gate_failed` (nothing switched) or `failed_rolled_back`
(previous selection restored and verified); 2 `needs_inspection` (an
unfinished switch could not be decided; nothing more is changed); 3 `refused`.
The commands take the deploy lock and refuse while a deploy window is
unfinished. A backup needs the selection record that `current` points to and
the object it names; status lists both.

Covered by offline fixture tests and a real-CLI gate test run on request
(`HUB_NATIVE_CLAUDE`, `HUB_NATIVE_CLAUDE_VERSION`); not yet by installed use.
