# Testing and acceptance strategy

Status: active  
Last updated: 2026-08-29

## Automated gate

The repository gate is:

```bash
python scripts/validate.py
```

It checks formatting, Ruff, Pyright, unit/integration tests, and publishable
configuration examples. Tests use fake Telegram/provider transports and
temporary Git/SQLite fixtures; they MUST NOT consume live provider tokens or
contact real Telegram groups.

Behavior changes follow test-first development as required by `AGENTS.md`.
Documentation-only changes require, at minimum, link/path sanity, secret scans,
and `git diff --check`; running the full repository gate is preferred.

## Live acceptance boundary

Live Telegram E2E is a separate, owner-coordinated operation because it changes
external conversation state and uses real identities/accounts. The next live
baseline is defined in the 2026-08-29 handoff and
`docs/product/PRODUCT_REQUIREMENTS.md` (`AC-F-002` through `AC-F-005`).

Do not substitute automated tests for these live observations, and do not run
the live E2E during unrelated documentation or implementation tasks. Never
weaken owner, project-root, sandbox, account, or approval allowlists for a test.

The initial Hub General baseline passed on 2026-08-29; see the
[live E2E record](2026-08-29-hub-general-live-e2e.md). Future behavior or
provider changes still require the relevant live canary rather than inheriting
that result indefinitely.

## Evidence rules

- Report exact commands and pass/fail summaries, not raw secret-bearing logs.
- Distinguish automated implementation coverage from live external acceptance.
- A provider probe proves provider availability, not full Telegram routing.
- A service restart proves only the state actually inspected after restart.
- Preserve bounded failure evidence; never attach tokens, state databases, raw
  rollouts, private invites, hidden reasoning, or terminal dumps.
