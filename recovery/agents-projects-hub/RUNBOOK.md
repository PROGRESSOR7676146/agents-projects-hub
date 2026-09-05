# Agents Projects Hub recovery capsule

Maintained for Hub `0.7.0`, state schema `21`. This capsule is a bounded local
recovery guide, not authority to weaken policy, retry ambiguous work, expose
private state, or perform a rollout.

## Safety boundary

- Diagnose read-only first. Do not restart all components as a bundle.
- Never edit SQLite queue/outbox tables or reset `executing`/`indeterminate`
  work to `queued`.
- Never copy a live SQLite file; use the Hub backup command/API.
- Do not delete sockets, locks, artifact spool files, releases, or rollback
  artifacts during first response.
- Hermes is an independent recovery channel, not an approval authority.
- Exact in-flight turns cannot be reconstructed after process or machine loss.

## Discover the live deployment

Do not assume a legacy shared `venv` exists. The current topology runs an
immutable release selected by each systemd unit. Resolve it from the unit:

```bash
systemctl --user show agents-projects-hub.service \
  -p ActiveState -p SubState -p ExecStart -p FragmentPath
systemctl --user cat agents-projects-hub.service
systemctl --user list-units 'agents-projects-hub*' --all
```

Take the absolute `agents-projects-hub` executable from `ExecStart` and use
that same executable for `release-info`, `status`, and `doctor`. Take the
configuration argument from the same argv; do not guess a path.

```text
ACTIVE_BIN release-info
ACTIVE_BIN status HUB_CONFIG
ACTIVE_BIN doctor HUB_CONFIG
```

Acceptance requires `release-info.ok=true`, `clean_tree=true`, an exact Git
SHA, schema `21/21`, and `deployment_revision.status=converged`. A live process
or package version alone is not deployment identity.

## Classify before repair

- Controller down: new ingress stops; committed queue work and other recovery
  channels remain.
- One worker down: only that provider stops taking work.
- Sender down: completed results remain durable; never repeat provider work to
  compensate for Telegram delivery.
- Monitor down: routing may still work, but automated health alerts stop.
- Mixed/unknown revision: stop promotion and use immutable manifest/rollback
  gates; do not reinstall from a dirty checkout.
- Schema/integrity failure: stop database users and preserve evidence before a
  reviewed restore. Ordinary runtime rollback retains schema-21 state.

Inspect the failed unit and bounded journal, then restart only that unit:

```text
systemctl --user restart agents-projects-hub.service
systemctl --user restart agents-projects-hub-worker@AGENT.service
systemctl --user restart agents-projects-hub-sender.service
systemctl --user start agents-projects-hub-monitor.service
```

After repair, rerun `release-info`, `status`, and `doctor`. Do not call the
deployment recovered until all required components report one identical clean
revision and the required checks pass. Live Telegram/provider acceptance is a
separate evidence level.

## Immutable rollback and deeper recovery

Use the full repository runbooks when the checkout is available:

- `docs/operations/QUEUE_RECOVERY.md`
- `docs/operations/IMMUTABLE_RELEASES.md`
- `docs/operations/LIVE_CANARY.md`
- `docs/operations/WSL_OFF_MACHINE_RECOVERY.md`

An immutable rollback requires the private deployment manifest, distinct
active/rollback wheels, their digests, configuration digest, consistent state
backup, and schema compatibility. Never synthesize these facts from filenames.

If the checkout is unavailable, preserve current services/state and restore
the repository from its remote or encrypted recovery set. Do not use this
capsule as a substitute for missing artifacts or private configuration.
