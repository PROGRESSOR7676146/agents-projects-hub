# tlive approval evidence and independent Codex lifecycle

Status: source candidate. Owner: Hub maintainer. Last verified base: `19e9c2704119e71deabcd59ea4c331a00cac54c6`.
Next trigger: independent exact-candidate review and canonical/hosted gates,
then separately authorized migration and live acceptance. Closure remains open.
The behavior is owned by [REQ-OPS-005](../product/PERSISTENCE_AND_RECOVERY.md)
and [ADR 0073](../decisions/0073-tlive-approval-evidence-and-server-custody.md).

## Offline source gate

Use the pinned archive, combined patch and build/test command in
[integration instructions](../../integrations/tlive/README.md). The owning CI
`tlive` job verifies the archive before extraction, applies the patch, builds and
executes the carried Node regressions, protected-host smoke and IPC adversarial
suite. Tests use fictional providers/adapters. IPC tests require socket binding;
a sandbox refusal is a failed/unavailable check, never a skip or passing E2E.
Focused Python selectors are `tests.test_tlive_extension`,
`tests.test_approval_surface`, `tests.test_approval_surface_state`,
`tests.test_approval_surface_monitor`, `tests.test_approval_surface_ipc` and
`tests.test_codex_server_service`, `tests.test_unix_listener_custody` and
`tests.test_maintenance_window`, plus `tests.test_unix_socket_diag`.
The published npm archive lacks tsconfig and plugin-lock tooling, so this is not
a full upstream typecheck/test claim. Dependency resolution is separate from
source-byte verification; CI installs dependencies with lifecycle scripts disabled.
The IPC suite starts at umask 022, applies the production drop-in umask, and
keeps Node asynchronous while a Python child parses fictional native frames
through `normalize_codex_activity`/`CodexActivityEvent` and `approval_metadata`.
It reads the actual daemon socket through `read_snapshot` and `assess_surface`,
requiring sent=1 for numeric and string request IDs, distinct typed identities,
private ownership/modes, matching config/socket paths and newline framing.
A passing run establishes agreement for supplied companion frames. Native
request-ID parity between separate app-server connections remains live acceptance.

Hub monitor output includes aggregate `approval_surface` counts when tlive
recovery is configured. It reads only `daemon.sock` beside that explicit tlive
configuration; it does not open the configuration or signing keys. A private
canonical parent and owned socket are required. The response budget is two
seconds and 128 KiB; snapshots older than five seconds, truncated output, changed
socket inode and malformed identities remain unknown. A native wait receives
30 seconds of initial diagnostic grace; a matched snapshot also preserves the
actual configured send grace (bounded to one day). Neither grace claims delivery.
A missing snapshot cannot establish recovery. An unchanged actionable condition
uses the existing operational-alert episode; it re-arms after confirmed recovery.

## Explicit independent-server migration

This procedure is local operator work. Source publication does not authorize
service actions, native calls, credential access or state mutation. Preserve
private exact-source, backup and rollback receipts outside Git.

1. Pin the candidate and compatible rollback artifacts, the native executable,
   Codex HOME, owning control socket and tlive daemon. Pin the actual native
   Codex binary, never a mutable npm wrapper whose vendor binary/PATH can change.
   Record attach-only source revision, archive/combined-patch manifest hashes,
   verified apply/build results and exact compiled daemon hash privately for
   both candidate and rollback. The service manifest pins the compiled artifact,
   not a source file or version label. Pinned runtime files must be read-only.
   Verify effective service units and namespace/force-free settings. Inventory all clients,
   worker slots, pending approvals, retained control sends and outboxes. Close
   native interactive clients explicitly. A green doctor or an idle heartbeat
   cannot prove this boundary. Use the existing worker drain procedure plus exact
   terminal/control and history evidence; unknown native controls block migration.
2. At a separately approved idle boundary, close admission and stop the old
   client/old server through their actual service ownership. Confirm original
   process incarnations and cgroup have exited and retained native history is
   intact. Do not use arbitrary process kills, SIGKILL or automatic retry.
   A leftover socket requires the separate owner-only procedure below; the
   automatic start guard preserves the path and refuses it.
3. Prepare a private mode-0600 `codex-server.json` with this shape (all values
   below are fictional placeholders):

   ```json
   {
     "version": 1,
     "hub_git_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "state_path": "/home/example/.local/state/agents-projects-hub/state.db",
     "codex_home": "/home/example/.codex",
     "socket_path": "/home/example/.codex/app-server-control/app-server-control.sock",
     "codex_binary": {"path": "/home/example/runtime/codex", "sha256": "REPLACE_WITH_EXACT_SHA256"},
     "tlive_daemon": {"path": "/home/example/runtime/tlive/dist/src/tlive-daemon.mjs", "sha256": "REPLACE_WITH_EXACT_SHA256"}
   }
   ```

   The guard opens only existing current-schema Hub state read-only. It refuses
   queued/in-flight work and saved results awaiting delivery, indeterminate work lacking exact retained
   terminal proof, unsettled control owners/claims, local writers, existing
   sockets or same-UID app-servers, unknown process custody and pin mismatches.
   An owner resolution alone does not substitute for native terminal evidence.
   It does not discover private configuration or read credentials.
4. Stage the independent unit (the installer only copies it) and explicitly
   install the reviewed `systemd/tlive.service.d/30-external-codex.conf` opt-in
   in the operator's unit drop-in directory. Preserve any stronger local
   namespace/permission restrictions. Verify effective tlive `UMask=0077` and
   an existing canonical, same-UID tlive home at mode 0700; umask cannot repair
   an existing public directory/socket. For a manual daemon launch, explicitly
   use umask 077 too. Unsafe permissions must remain unknown to `read_snapshot`,
   never be waived. Align every client with the same explicit Codex HOME/socket.
   The service intentionally has no automatic restart or install target;
   tlive neither starts nor stops it.
5. With old clients still quiesced, generate an exact start receipt from the
   installed clean Hub runtime and save stdout privately with umask 077:

   ```sh
   python -m hermes_codex_router.codex_server_service plan \
     --manifest /home/example/.config/agents-projects-hub/codex-server.json \
     --assert-native-clients-closed
   ```

   Save it as `codex-server-start.json` beside the manifest. It expires after
   60 seconds and binds the complete manifest; it is a local assertion, not
   independent native idle proof. Explicitly start only
   `agents-projects-hub-codex-app-server.service` within that interval. Its
   start path checks the receipt, pins, exact clean Hub revision and idle
   preconditions again before exec. A refused or unknown outcome requires
   inspection, never a blind repeat or new receipt against unknown work.
   Keep admission closed and all client/worker/control owners quiesced through
   startup and custody verification. The read-only SQLite snapshot, process
   inventory and short-lived receipt do not lock admission or prevent a client
   from reconnecting; concurrent admission invalidates this procedure's premise.
6. Verify the new server's exact executable/process/cgroup/socket custody before
   starting the attach-only tlive client. Confirm every required component's
   exact revision, retained thread/history and unknown-control fences. A client
   reconnect does not establish pending approval recovery. Perform the separately
   authorized bounded human Allow/Deny and client-restart acceptance; preserve
   request/item identities, actual card receipts and denied effects privately.

### Failure-safe maintenance wrapper

For the next procedure, enclose **every service stop and subsequent diagnostic**
in `maintenance_window.MaintenanceWindow`. Do not reuse a consumed activation
script or put restoration only after the last successful check. The wrapper is
for this explicit maintenance operation, never a monitor or automatic updater.

Supply a fixed reviewed `Unit` list of consumers (attach-only tlive, workers,
sender and previously active diagnostics as applicable), then admission units.
Exclude the independent native server: its start is a separately guarded,
single-use step of the main procedure. Snapshot all units before effects;
initially inactive units remain inactive. Close admission before stopping
consumers through `window.stop(name)`. Each callback has a finite deadline;
its private receipt records effect intent before submission. No pending
supervisor job, physical old-incarnation exit and empty cgroup are required
before reporting `inactive`. Unavailable observations are `unknown`.

The procedure uses this structure (deployment callbacks and identities stay
private):

```python
from dataclasses import asdict
from hermes_codex_router.maintenance_window import MaintenanceWindow


def guarded_cutover(reviewed_units, cutover, persist_result):
    window = MaintenanceWindow(reviewed_units)
    primary = None
    try:
        with window:
            # cutover must use window.stop for every eligible unit.
            # Preserve the separate fresh native-start/idle/backup gates.
            cutover(window)
    except BaseException as error:
        primary = error
        raise
    finally:
        result = {
            "primary_failure_type": None if primary is None else type(primary).__name__,
            "restoration": asdict(window.report),
            "restoration_complete": window.report.complete,
            "status": (
                "COMPLETED" if primary is None and window.report.complete else "INSPECT_NO_RETRY"
            ),
        }
        try:
            persist_result(result)
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note("Failed saving maintenance report: " + type(error).__name__)
```

Restoration verifies prerequisites and readiness, and starts only initially
active units whose stop this operation attempted. It verifies an already
running unit without another start. Consumers precede admission; any required
consumer failure keeps admission closed, while independent safe restoration
continues. Every consumer prerequisite must verify compatible current config,
worker safety and (where applicable) unchanged native custody. For tlive it
also proves effective attach-only mode. Never use general liveness as readiness.
An uncertain start is observed, never retried. Preserve the primary error and
separate restoration report; incomplete restoration is a failed maintenance
result requiring explicit recovery, never success or silent waiting.
The report retains initial states, stop attempts, per-unit outcomes and an
explicit phase. Interruption during finalization leaves `interrupted` with
`complete=False`, preserves any primary failure and never authorizes a retry.
Persist `restoration_complete` explicitly: `asdict` does not include properties,
and phase `finished` alone does not mean every unit became ready. If saving the
report fails, preserve that failure alongside the primary error; neither failure
permits replay. Durable pre-effect receipts remain authoritative for attempted
starts, including an interrupted or unobserved outcome.

Use `unix_listener_custody.verify_listener` for passive host socket evidence.
Compare the expected PID, boot ID, start ticks, executable, UID and cgroup with
reviewed pins before passing the identity. The helper selects exactly one Unix
stream **listener** using a bounded binary Linux `SOCK_DIAG` dump, binds its
VFS device/inode to the current owned filesystem socket and verifies the kernel
inode in that process's FDs twice, rechecking listener and incarnation. Text
`/proc/net/unix` paths can contain unescaped newlines and are not custody
authority. Accepted connections may share a pathname and are not additional
listeners. The verifier requires the same network namespace as the server;
missing binary evidence, a different namespace or a replaced path refuses.
Resolve any approved socket alias separately; a symlink is not accepted as the
kernel filesystem path. Keep directory, artifact and effective-unit checks in
the private wrapper; no socket removal,
native restart, inference, database rollback or uncertain work replay follows
from this diagnostic.

Before arming a new procedure, inject an error after attach and before normal
service return in an offline fixture using these production helpers. Require
consumer return before admission, independent restoration after one failure,
no duplicate start, unknown-custody refusal and an explicit incomplete result.
`tests.test_maintenance_window` and the real local multi-connection Unix fixture
are the reusable regression gate. They do not establish live deployment
acceptance. Python finalization cannot survive SIGKILL, interpreter loss or a
host crash: the private pre-effect receipt remains authoritative for inspection
and explicit recovery; never auto-replay the procedure after such an exit.

## Stale socket after unclean shutdown

This is explicit local incident work by the installation owner (or an explicitly
assigned trusted recovery operator), outside `plan`/`run`. The guard never deletes
or kills. After **every reboot**, the external server remains unavailable until
an explicit fresh **plan + start**; there is no automatic restart. Preserve the
incident, exact old boot/PID/start-tick/cgroup identities and uncertainty privately.

1. Close Codex admission and quiesce all client/worker/control owners using the
   [drain and cleanup boundaries](WORKER_DRAIN_PREPARATION.md#explicit-local-candidate-workflow).
   Stop the exact owning units through the service manager in separately authorized work.
   Verify supervisor state is confirmed inactive, no pending start/restart, empty
   cgroups, and physical exit of each recorded process incarnation. PID absence
   alone is insufficient; compare boot ID and `/proc/PID/stat` start ticks to
   prevent PID reuse. A changed boot ID excludes only the old incarnation.
   Unavailable supervisor or host `/proc` visibility means stop, not cleanup.
2. Inventory **all current same-UID processes** through host `/proc`, including
   command argv and cgroups privately. `refuse_existing_servers()` below performs
   the conservative app-server scan; it does not prove client/control quiescence.
   Any same-UID app-server, unreadable live process, surviving owner, native
   client or possible concurrent launcher blocks cleanup. Keep launchers stopped
   throughout inspection/removal; this assumes trusted same-UID cooperation.
3. Inspect the exact manifest socket under the canonical owned private Codex
   home/control directory. Record its device, inode and owner privately using
   `stat -c '%d %i %u %a %F' -- SOCKET`. It must be an owned non-symlink Unix
   socket, not an arbitrary path. A bounded AF_UNIX stream connect must fail with
   **ECONNREFUSED**; success, timeout, ENOENT or any other error is not stale proof.
4. Only the owner may explicitly remove that reviewed inode. With the previous
   conditions still held, run this local snippet using the installed Hub Python,
   replacing only the fictional path and reviewed `(device, inode)` pair. It
   rechecks private directory custody, same-UID inventory, ECONNREFUSED and exact
   socket identity immediately before descriptor-relative unlink. A changed
   directory/path/inode or any exception aborts; never substitute `rm -f`.

   ```python
   import os, socket, stat
   from pathlib import Path
   from hermes_codex_router.codex_server_service import refuse_existing_servers

   path = Path("/home/example/.codex/app-server-control/app-server-control.sock")
   expected = (123, 456)  # Replace with the privately reviewed device and inode.


   def private_directory(p):
       s = p.lstat()
       if (
           p.resolve() != p
           or not stat.S_ISDIR(s.st_mode)
           or s.st_uid != os.getuid()
           or s.st_mode & 0o077
       ):
           raise RuntimeError("directory custody unavailable")
       return s.st_dev, s.st_ino


   private_directory(path.parent.parent)
   parent = private_directory(path.parent)
   fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
   try:

       def exact_socket():
           if private_directory(path.parent) != parent:
               raise RuntimeError("directory changed")
           d = os.fstat(fd)
           s = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
           if (
               (d.st_dev, d.st_ino) != parent
               or not stat.S_ISSOCK(s.st_mode)
               or s.st_uid != os.getuid()
               or (s.st_dev, s.st_ino) != expected
           ):
               raise RuntimeError("socket changed or unowned")

       refuse_existing_servers()
       exact_socket()
       with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
           client.settimeout(2)
           try:
               client.connect(str(path))
           except ConnectionRefusedError:
               pass  # Only ECONNREFUSED qualifies.
           else:
               raise RuntimeError("socket has a listener")
       refuse_existing_servers()
       exact_socket()
       os.unlink(path.name, dir_fd=fd)  # Explicit owner action, never guard cleanup.
   finally:
       os.close(fd)
   ```

5. Generate a **new** plan only after cleanup; all idle/uncertainty/control gates
   still apply. Socket removal or server disappearance proves no native turn
   terminality. Retain current state/history, origin/root binding and send fences.

If `leased`/`executing` or unproven `indeterminate` work blocks the new plan, keep
Codex unavailable and assign the incident to the installation owner, with the Hub
maintainer responsible for a reviewed recovery path. Existing supported exact
terminal reconciliation uses native read-only protocol on an available owning
server; it cannot break this absent-server/start-gate dependency. There is no
supported offline terminal-proof import here. Owner resolutions, reboot/PID exit,
a copied transcript or direct SQLite edits cannot satisfy this service gate.
Exit requires supported durable reconciliation leaving no queued, leased,
executing or result-ready Codex rows; retained exact thread/turn/root terminal
evidence for indeterminate rows; settled controls and quiescent writers, so the
unchanged guard passes,
or a separately reviewed and authorized recovery implementation that can obtain
that proof without productive replay. Until then, record the availability blocker
and preserve uncertainty; do not launch a substitute server/stdio writer on the
same root or invent evidence. Stale-socket cleanup solves only path custody.

## Rollback and acceptance limits

Stop admission first. Keep the external server running when rolling back only
tlive. The target must be a source/hash-verified **attach-only-capable build**
(this combined patch or a reviewed successor); unpatched **5.3.1 is incompatible**.
The environment setting alone does not work on unpatched 5.3.1: on a failed probe
it can spawn a competing server. Verify the target source provenance/build and
compiled `tlive_daemon` hash against the private service manifest before switching,
and keep attach-only enabled. Without such a target, tlive rollback is blocked;
retain the verified build and assign the incident to the installation owner.
Retain current SQLite state and unknown evidence;
do not restore a pre-change database over newer work. Never roll back the server
while work or send ownership is active/unknown. Server rollback requires a new
idle/custody gate, exact retained compatible executable/history and fresh receipt.
A stale socket or surviving process blocks it just as it blocks forward startup.

Required live evidence remains: real permission-card receipt/visibility, human
Allow-once/Deny, duplicate/stale controls, send outage/ambiguity, tlive-only restart
preserving the exact external server incarnation and history, fresh daemon epoch,
and unchanged native uncertainty/no replay. Also verify typed request/item IDs
observed by Hub and companion on **separate native connections** agree for each
genuine approval; the offline shared-frame fixture does not prove per-connection
server identity semantics. Pin the tested native Codex binary for that check.
Offline fixtures establish source and synthetic-fault behavior only. This procedure does not claim a host migration,
recovered pending approvals, live subscription billing or cold-boot acceptance.
