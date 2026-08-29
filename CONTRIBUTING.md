# Contributing

Contributions are welcome, especially tests, portability improvements, and
adapter contracts that preserve the security invariants.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the complete local gate before opening a pull request:

```bash
ruff format --check .
ruff check .
pyright
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 -m hermes_codex_router.cli \
  validate config/projects.example.json --allow-missing
```

The gate begins with a repository privacy scan. It rejects internal histories
and handoffs, non-example identities and paths, private Telegram identifiers,
session dumps, and common secret-bearing runtime files. Never weaken the scan to
make a PR pass; replace deployment-specific material with fictional examples.

## Safety requirements

- Add tests before changing router behavior.
- Do not accept filesystem paths, sandbox modes, or approvals from Telegram.
- Keep subprocess commands as argument arrays; do not introduce `shell=True`.
- Never include credentials, local config, state databases, rollout files,
  terminal buffers, hidden reasoning, conversation exports, real project names,
  account hints, bot usernames, chat IDs, or owner-specific paths in commits or
  fixtures.
- Use fake external services in automated tests. Live deployment and credential
  changes require a separately reviewed operator action.
- Keep one writer per provider session and one worktree per concurrent lane.

New third-party source must be license-compatible with MIT and recorded in
`ACKNOWLEDGMENTS.md` or a dedicated notice file.
