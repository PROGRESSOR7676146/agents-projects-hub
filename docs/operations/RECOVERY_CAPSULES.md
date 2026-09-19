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

## Schema consumers and optional model proxies

The Hermes Hub plugin and turn-export hook are database consumers, even though
Hermes inference and its private channel are independent. For every schema
rollout or rollback, include the idle gateway in the consumer inventory. Point
`HERMES_PROJECT_HUB_SOURCE` at the same clean immutable release as Hub, restart
the affected gateway and require `hermes:hub_plugin_compatibility` to pass.
Doctor reads bounded build/schema metadata without importing foreign code or
printing process environment. This optional-channel warning does not stop Hub,
but a deployment must not be accepted while it is mismatched.

Never repair a deployed Hub by rerunning the bootstrap installer: it refuses an
existing unit before mutation. Legacy multi-auth ordering templates are not
installed automatically. Do not restore a Hermes PATH shim from old capsules.
Check the current owner guide, not only the validity of an old capsule's hashes.

An optional CLIProxyAPI route belongs to Codex's local provider configuration;
it must not become a mandatory Hub/Hermes service dependency. Keep a separately
verified direct Codex launcher and Hermes's native providers and credentials.
Proxy recovery must preserve immutable thread identity, writer leases and
uncertain work. Management, client and upstream credentials are distinct;
never copy or print them. Monitoring remains passive, without live model probes.
