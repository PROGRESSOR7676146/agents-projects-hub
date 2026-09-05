# Immutable release manifest and schema gate

Status: repository tooling implemented; live activation not authorized
Last updated: 2026-09-05

The deployment manifest is private operational evidence. It binds two distinct
clean-tree wheel artifacts—candidate and rollback—to the exact configuration,
consistent state backup, and schema transition. It contains private paths and
therefore must never be committed.

## Artifact contract

Each wheel is inspected without installing or importing it. The gate requires:

- one wheel metadata version matching the embedded package version;
- a complete clean-tree build identity with exact Git SHA and timezone-aware
  build time;
- literal minimum and maximum supported SQLite schema versions;
- an exact SHA-256 digest of the wheel bytes.

The active and rollback wheels must be distinct releases. The active wheel must
support the backed-up schema and its own target schema. The rollback wheel must
also support that target schema, because ordinary runtime rollback retains the
migrated database and all accepted queue/outbox records.

## Create and verify

Prepare the two wheels, a mode-`0600` shadow configuration, and a
SQLite-consistent mode-`0600` backup outside the repository. Then create the
manifest without opening live state:

```bash
agents-projects-hub release-manifest create PRIVATE_MANIFEST \
  --active-artifact CANDIDATE_WHEEL \
  --rollback-artifact ROLLBACK_WHEEL \
  --config SHADOW_CONFIG \
  --backup STATE_BACKUP
```

Creation is exclusive: it refuses to overwrite an existing manifest and writes
the new file as mode `0600`. Verify all bound bytes and compatibility again:

```bash
agents-projects-hub release-manifest verify PRIVATE_MANIFEST
```

After a migration on a disposable copy, add `--state DISPOSABLE_STATE` to prove
that its exact `user_version` equals the manifest target and remains readable by
both artifacts. This option is read-only; it does not perform the migration.

## Stop conditions

Stop before activation if either wheel is dirty, identities or digests differ,
the configuration or backup changed, SQLite integrity is not `ok`, the backup
schema is newer than the candidate target, or the rollback artifact cannot read
the target schema. Do not compensate by restoring the pre-migration backup for
an ordinary runtime rollback, weakening the manifest, or rebuilding an older
release from a mutable checkout.

Manifest verification proves artifact identity and schema compatibility only.
It does not prove queue drain, service convergence, Telegram behavior, or a
live rollout.
