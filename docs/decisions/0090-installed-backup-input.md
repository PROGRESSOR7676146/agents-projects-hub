# ADR 0090: Native installed deployment input for backup

Status: accepted direction; first source slice, adoption pending
Date: 2026-11-06

## Decision

Extend existing release-manifest/backup entries with a versioned private installed
input and passive readback. The installation owner supplies accepted wheel
provenance, sealed runtime pins and configuration/consumer bindings. Derive
version/schema from verified artifacts and topology from Hub configuration.
Observed processes verify the record; they never become a new trust root.
Reject unknown or unsupported inputs explicitly.

Allow schema-compatible mixed Hub consumers for SQLite backup while retaining
mixed-revision health warnings. Keep an independently owned native app-server
identity separate. Foreign DB consumers need their owning compatibility contract;
a non-state auxiliary declaration cannot replace it.

Reuse SQLite's consistent backup API. A new directory records original intent
before copying and publishes a verified receipt after readback. Retain unfinished
originals, refuse replay and allow read-only inspection after a lost response
without the latest installation. Runtime rollback retains the current database
and work accepted after an update.

## Boundaries and trade-offs

The first slice captures SQLite only and explicitly reports missing filesystem
recovery scope. Full application recovery is not claimed. Host archives stay
externally owned; no scheduler, foreign config editor or maintenance framework.
Existing preactivation manifests retain their format/meaning. Active inventory,
product integration adapters and installation-owned record generation are
subsequent slices, not implied by successful static readback.

The [owning runbook](../operations/INSTALLED_BACKUP.md) defines supported input,
commands, failure/inspection behavior and remaining installation acceptance.
