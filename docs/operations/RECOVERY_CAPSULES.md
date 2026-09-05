# Reciprocal recovery capsules

Hub and Hermes are independent recovery channels. Each project owns a small,
credential-free runbook and publishes it from a clean Git revision into:

```text
~/.local/share/recovery-capsules/PROJECT/versions/GIT_SHA
~/.local/share/recovery-capsules/PROJECT/current
```

The receipt binds the owner revision, clean-tree flag, UTC publication time,
and SHA-256 hashes. `current` is replaced atomically. A peer reads but never
edits or republishes the owner's capsule. This shared storage creates no
runtime dependency: the last verified guide is readable while either service,
source checkout, or virtual environment is unavailable.

Before Hub-side recovery of Hermes, verify and read the Hermes-owned guide:

```bash
scripts/verify-recovery-capsule.py hermes
less ~/.local/share/recovery-capsules/hermes/current/RUNBOOK.md
```

Stop on a missing, stale, dirty, tampered, or escaping generation. The capsule
is procedure only—not an approval, executable recovery artifact, secret store,
state backup, or evidence of live acceptance.

The owner must republish within 30 days and whenever service topology,
release/schema, restore commands, or safety boundaries change. Installer
publication plus peer-side daily auditing provides the normal exchange loop;
Git review remains the maintenance and provenance boundary.
