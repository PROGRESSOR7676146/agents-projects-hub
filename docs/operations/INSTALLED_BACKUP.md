# Installed deployment backup input

Status: first source slice; installation publication and the external archive
adapter remain pending. Contracts: [REQ-OPS-010/012](../product/PERSISTENCE_AND_RECOVERY.md).
Rationale: [ADR 0090](../decisions/0090-installed-backup-input.md).

Source owner: primary development agent; lane `feat/installed-backup-contract`;
base: integrated prepared-release-switcher source. Exact-source canonical,
hosted and independent review gates remain required. Closure requires the
separate installed/external acceptance below; this source slice cannot close it.

## Ownership and scope

Hub owns its configuration, schema, installed files and supported consumer
inventory. The external archive owner calls this native entry; it owns archive
encryption, remote storage, scheduling, retention and restore acceptance. This
entry changes none of that owner's files and creates no scheduler. Passive
inspection runs service-manager queries, filesystem reads and read-only SQLite
schema queries, without provider calls or service actions.

This increment produces **SQLite-only** snapshots. Its receipt explicitly says
`complete_recovery_set: false` and lists uncaptured incoming/artifact spool roots.
It is not an encrypted application recovery set or a complete externally verified
archive. Each database is independently consistent; no cross-database transaction
or filesystem-spool consistency is claimed.

## Private installation record

The owning install procedure supplies a canonical regular mode-0600 JSON file
outside Git. Its kind is `hub-installed-deployment`, `schema_version` is 1, and
it has exactly these other fields:

| Field | Meaning |
| --- | --- |
| `configuration` | Primary installed configuration file descriptor. |
| `components` | Hub consumer records described below. |
| `auxiliary` | Independently owned, non-Hub-state consumers. |
| `recovery_artifacts` | Additional exact retained file descriptors. |

A file descriptor has only `path` (canonical absolute path) and `sha256`.
The release procedure obtains these from native `file_descriptor`; it does not
copy versions/schema into this record. This is an accepted local input under the
trusted-host model, not a signature, downloaded release trust root, installation
receipt, or proof of a successful cutover. Do not synthesize it from arbitrary
discovered processes. Active-record generation/publication is a subsequent
installation slice; the external backup must not become its editor.

Each Hub record has exactly `component`, `instance_id`, `unit`, `configuration`,
`release_manifest`, `artifact`, `package_root`, `argv`, `executable` and `runtime`.
Configuration, executable and release-manifest references are file descriptors.
`artifact` selects `active` or `rollback` from the existing verified
[deployment manifest](IMMUTABLE_RELEASES.md). `runtime` contains a canonical
installed root and its sealed-tree SHA-256. `package_root` must be its
`venv/lib/python*/site-packages/hermes_codex_router` directory. Exact package
files must match the selected wheel; the complete sealed runtime reuses the
existing structured [tree digest](PREPARED_RELEASE_SWITCHER.md).

Supported launch: the pinned runtime's `venv/bin/python -I -B -m
hermes_codex_router.cli`, then the real command and installed config. Worker
agent/slot arguments must match the declared identity. The install procedure
must configure that launch; this entry never repins or restarts a process.
Controller, optional monitor, configured sender/provisioner and every external
worker slot are derived from configuration. No credentials are loaded.
Unexpected active Hub services, duplicate/missing consumers, incompatible state
paths and unknown direct-message databases refuse.

An auxiliary record has `unit`, `owner`, `required`, `uses_hub_state`, `argv` and
an `executable` file descriptor. Owners are `codex/tlive`, `cpa` or `hermes`;
`uses_hub_state` must be false. The reader checks its pinned executable and
exact process incarnation/arguments without assigning a Hub release identity.
An external **Hub-state** consumer needs its supported compatibility integration;
this first format refuses such a declaration. This slice does not certify an
installation containing a Hermes Hub DB plugin/hook or direct provider daemon
omitted from supported inventory. Never relabel a DB consumer as an auxiliary.
Those adapters must be delivered before that installation can adopt the input.

## Inspect and snapshot

Run from a reviewed installed executable:

```bash
agents-projects-hub release-manifest installed PRIVATE_INSTALLATION_RECORD
agents-projects-hub backup INSTALLED_STATE NEW_PRIVATE_DIRECTORY \
  --installed-manifest PRIVATE_INSTALLATION_RECORD
agents-projects-hub release-manifest inspect-backup NEW_PRIVATE_DIRECTORY
```

`installed` is read-only. It returns versioned installed files, native per-component
release/schema ranges, configured databases and observed process incarnations.
It compares service-manager PIDs and Linux process start markers before/after
readback. Only accepted artifacts establish Hub revision; a found process path
cannot add a release. Output replaces process arguments with a digest.

Mixed Hub revisions remain visible. Backup compatibility means accepted Hub
consumers can read each retained schema. It does not prove general protocol
compatibility, healthy routes, live acceptance or convergence. Existing mixed/
unknown health alerts remain unchanged. Independent app-server ownership imposes
no artificial Hub version requirement or restart.

In installed mode the destination is an **exclusive new directory**. Supplied
state must match the installation's shared DB. After input verification the entry
saves immutable `PENDING.json`, uses native SQLite backup with a bounded busy
deadline, and checks schema plus the complete installation fingerprint again.
Only then does it fsync `receipt.json`. Without the flag the legacy file backup
invocation retains its behavior.

A failed/interrupted operation keeps its directory and PENDING. Another invocation
on that directory refuses; it never resumes, deletes or overwrites it. Inspection
checks original snapshots against the receipt without the current installation.
A lost response can therefore be inspected after another upgrade. Malformed or
partial receipts refuse; an absent receipt remains pending. New Hub publication
does not change another product's PENDING or authorize replay.

## Evidence and remaining delivery

`tests.test_installed_backup` exercises consecutive isolated schema/config-path/
worker-count transitions, mixed revisions, busy/drifted inputs, interruption,
unknown consumers and lost-response inspection. `tests.test_installed_processes`
reads a real owned fictional process with a fake service-manager mapping, plus
restart/unavailable-manager refusal. These are synthetic/native component tests,
not an installed update.

Next: product-specific compatibility adapters and installation-owned generation;
active inventory publication only after verified install outcomes; dependency-aware
owned-storage planning/cleanup; complete version-bound recovery package. Do not
invent retention durations. The external owner implements its consuming adapter.
Actual archive, isolated restore and a natural scheduled run require separately
authorized installation and joint acceptance.
