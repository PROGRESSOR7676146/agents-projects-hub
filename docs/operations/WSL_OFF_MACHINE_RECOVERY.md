# Off-machine WSL backup and cold-restore drill plan

Status: accepted plan; backup automation and cold drill not executed
Last updated: 2026-09-05

This is a separate machine-loss plan, not a rollout procedure. Executing it
against private operator data, stopping WSL, writing to an external repository,
or importing/removing a WSL distribution requires a separately authorized
maintenance window. No such action is part of repository release preparation.

## Recovery outcome

A successful recovery must rebuild the product on replacement hardware from an
off-machine copy while preserving the last accepted durable state and keeping
all messaging/provider ingress disabled until identity and safety gates pass.
The drill proves restore mechanics on an isolated disposable host. It does not
prove recovery of an in-flight provider turn, and it never converts an
`indeterminate` job into retryable work.

The backup design uses two independent layers:

1. frequent encrypted, versioned application-level snapshots for short recovery
   point objectives;
2. a periodic cold WSL export after coordinated shutdown for a coarse complete
   system image.

Both copies must leave the physical source machine. A Windows-host folder,
another partition on the same SSD, a continuously synchronized folder, or one
cloud mirror without retention is not an off-machine backup.

## Recovery-set inventory

| Set | Required content | Restore evidence |
| --- | --- | --- |
| Immutable release | Candidate, active, and compatible rollback wheels; private deployment manifests; exact Git SHAs and artifact digests | `release-manifest verify`; installed `release-info`; wheel SHA-256 inventory |
| Hub durable state | SQLite-consistent Hub databases and backup files; artifact spool needed by retained outbox rows; private registry/config/environment files | SQLite integrity and schema; bounded table counts/status digest; restrictive modes; outbox file digest match |
| Provider continuity | Provider-owned session stores and private runtime homes for configured providers; independent Hermes/tlive state required for recovery | Filesystem inventory and modes; session identifiers present; no provider call during the drill |
| Projects | Every project repository, worktree, submodule/LFS object needed offline, plus committed, uncommitted, staged, and untracked files | `git fsck`; refs/worktree inventory; digest inventory for non-committed content |
| Bootstrap | WSL configuration, user-service unit/drop-in inventory, enabled-unit list, tool/runtime lockfiles, and reproducible install notes | Fresh-host checklist; unit files resolve only to immutable release paths |
| Secrets | Bot/provider credentials and recovery keys only inside the encrypted private recovery set | Decryption test; file-mode audit; no secret values in logs or manifests |

Do not back up live sockets, PID/lock files, Python bytecode, build directories,
or reproducible virtual environments as authoritative state. Do not copy a live
SQLite main file plus `-wal`/`-shm` sidecars as an improvised backup. Use the
SQLite backup API and capture the artifact spool at the same quiesced recovery
point whenever prepared outbox documents exist.

## Application-level backup workflow

The eventual automation must run locally and emit bounded private evidence:

1. Preflight the destination, encryption key availability, free space, last
   successful snapshot age, and repository lock. Never print credentials,
   filenames from private projects, or provider session IDs to shared logs.
2. For a crash-consistent routine snapshot, use the Hub SQLite backup command
   while services continue. For an accepted recovery point containing queue,
   outbox, and artifact-spool state, enter a separately approved quiesce window:
   stop new admission at a polling boundary, stop workers/sender at their safe
   boundaries, and classify leases without retrying ambiguous work.
3. Preserve every existing `indeterminate` row exactly as data. Backup is not
   reconciliation: do not reset, delete, retry, acknowledge, or renumber any of
   those rows.
4. Create SQLite-consistent snapshots, run `PRAGMA integrity_check`, record
   schema versions, and calculate a bounded digest over durable work identity
   and status. Copy any spool files referenced by retained outbox parts in the
   same recovery point and verify their recorded size/SHA-256.
5. Snapshot the other recovery sets with encryption, content checksums,
   versioned retention, and atomic completion markers. An interrupted snapshot
   never becomes the latest-good recovery point.
6. Verify a sample read/decrypt from the off-machine destination, record the
   snapshot ID and age privately, then resume only the components deliberately
   stopped for this backup.

The backup engine may be restic, Borg, or an equivalent maintained encrypted
content-addressed tool. Selection is a deployment decision; repository code
must depend on its documented exit status and machine-readable verification,
not scrape presentation text. Recovery keys must be stored separately from both
the laptop and backup repository.

## Cold WSL export

A cold export supplements rather than replaces application snapshots:

1. Complete the accepted application-level recovery point first.
2. Record which WSL distributions will be affected. `wsl --shutdown` stops all
   distributions, so obtain explicit authorization and verify no unrelated
   work is active.
3. Shut WSL down cleanly from Windows and confirm the target distribution is no
   longer running.
4. Export it with the supported Windows WSL export command to an encrypted
   staging destination, then transfer the archive or VHDX off-machine.
5. Hash the completed export, verify the remote copy, and retain its matching
   application-snapshot ID. Never declare success from command exit alone.

Keep at least two generations and one geographically independent copy. Apply a
retention policy that protects the newest verified application snapshot, the
newest verified cold export, and the prior known-good generation from automatic
pruning.

## Cold-restore drill

Run at least quarterly and before a persistence or host-platform cutover. Use a
disposable replacement machine or VM whose network is blocked before the first
restored WSL boot. A second distribution on the production laptop is weaker
evidence and is not an off-machine drill.

1. Select one application snapshot and its paired cold export without using the
   source machine. Start the recovery clock and verify checksums before import.
2. Import under a new conspicuously drill-only distribution name and a new
   storage location. Keep host/hypervisor networking denied. Modern WSL may
   start enabled systemd user units on first launch; network isolation is the
   first safety boundary, not an in-guest command run afterward.
3. On first offline boot, disable Hub, monitor timers, provider ingress,
   workers, sender, Hermes Gateway, tlive, and optional account/proxy services.
   Quarantine token-bearing environment/config files from their runnable paths.
   Prove no process owns Telegram polling or a provider session.
4. Restore the application snapshot into staging paths, not over the imported
   image blindly. Reapply directory/file ownership and restrictive modes.
5. Verify immutable wheels and manifests. Install the exact selected wheel into
   a new environment and require `release-info` to report the recorded clean
   SHA. Run `release-manifest verify --state` read-only against a disposable
   copy of restored state before any normal state open.
6. Verify every SQLite database with integrity and schema checks. Compare the
   recorded durable-work digest and bounded counts by status. All restored
   indeterminate rows must remain indeterminate with identical job identity,
   attempt count, and error classification. Verify retained outbox parts against
   their spool size/SHA-256 without sending them.
7. Verify project and provider continuity offline: run `git fsck`, compare refs,
   worktrees and non-committed-file digests, and confirm provider session stores
   exist with safe modes. Do not log in, resume a provider, or infer session
   usability merely from files being present.
8. Run the repository canonical validation and the synthetic
   `release-dry-run` using fictional generated state. Do not run Telegram E2E,
   start production services, or allow outbound provider access.
9. Produce a private drill report with snapshot/export IDs, source and restored
   hashes, schema versions, immutable release identities, recovery duration,
   missing items, and pass/fail for every gate. Store no private drill evidence
   in this repository.
10. Keep the disposable restore until the report is reviewed. Removing the
    imported distribution is a separate destructive, explicitly confirmed
    action; deletion is never part of the test command itself.

## Acceptance and stop conditions

The drill passes only when it is performed without the source machine, all
required sets restore, all integrity/digest checks match, candidate and rollback
artifacts both support the restored schema, all indeterminate work remains
unchanged, and no messaging/provider/service action occurred. Record measured
recovery point and recovery time; do not invent target values before the first
timed drill supplies evidence.

Stop and retain the drill environment when any checksum differs, a backup is
not decryptable, a SQLite check fails, an artifact identity is dirty/unknown,
the rollback wheel rejects the schema, an outbox spool file is absent, a project
has missing objects, or any restored service attempts network access.

Actual disaster promotion is a different procedure. Before enabling a restored
host, prove the old host cannot run, rotate/revoke credentials as appropriate,
review stale writer leases locally, create a new consistent backup, execute the
controlled rollout, and perform deployment-local acceptance under separate
authorization.

## Automation backlog

- Machine-readable recovery-set inventory with per-set age and digest status.
- Scheduled encrypted application snapshots with one edge alert per failure
  episode and re-arm after a verified success.
- Windows-side cold-export wrapper with explicit WSL shutdown confirmation and
  off-machine copy verification.
- Offline restore verifier for modes, artifacts, SQLite schema/integrity,
  durable-work digests, outbox spool bindings, Git objects, and service-disable
  state.
- Drill report template with measured RPO/RTO and zero-network/service proof.
