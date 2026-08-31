# Testing and acceptance strategy

## Automated gate

Run:

```bash
python scripts/validate.py
```

The gate performs the repository privacy scan, formatting, Ruff, Pyright,
unit/integration tests, and publishable configuration validation. Automated
tests use fake transports and temporary Git/SQLite fixtures. They must not
contact real Telegram groups or consume provider tokens.

`tests/test_fault_injection_matrix.py` is the subprocess queue acceptance gate.
It uses marker-synchronized fictional child actors, bounded parent waits, and
forced process termination to join real Controller polling, SQLite recovery,
isolated workers, and the standalone sender. Lower-level state-machine tests
remain in their focused modules.

## Privacy gate

`python -m hermes_codex_router.privacy_scan . --history` scans both the proposed
tree and every reachable Git blob plus commit/tag metadata. It rejects:

- files under `docs/history/` or `docs/handoffs/`;
- non-example email addresses, home paths, bot usernames, Telegram chat IDs,
  private invites, and bot tokens;
- raw agent/session transcript markers;
- local configuration, database, key, socket, session, and log files;
- private deployment fingerprints retained only as one-way hashes.

False positives must be resolved by using conspicuously fictional fixtures, not
by allowlisting real deployment data.

## Live acceptance boundary

Live Telegram acceptance is a separate owner-coordinated operation because it
changes external state and uses real identities/accounts. Store its transcript,
IDs, account hints, screenshots, and service logs outside Git. Public status may
state only the reusable behavior tested and the kind of acceptance required.
