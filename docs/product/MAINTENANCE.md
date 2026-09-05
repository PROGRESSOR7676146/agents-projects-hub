# Maintenance requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 18. Known limitations

- Live acceptance is point-in-time evidence and must be repeated after material
  routing, provider, Telegram-policy, or persistence changes.
- Exact provider capability and resume flags can change between CLI versions.
- OpenCode and Antigravity lack a tlive-equivalent semantic remote companion;
  tmux/PTY remains the low-level fallback.
- Hermes native local transfer capability needs separate confirmation before a
  common `/local` experience can claim provider parity.
- Antigravity automatic rotation is unavailable without a supported headless
  account-pool interface.
- The current recovery plane handles component/service failure on the existing
  machine. An off-machine WSL backup and cold-restore drill is now specified,
  but its automation and first private drill remain planned; exact recovery of
  an in-flight provider turn remains impossible.
- Each locally managed provider currently has one execution slot across all
  projects. A long turn can delay that same provider in another topic, while
  deterministic Hub commands and unrelated providers remain available.
- Topic creation depends on the deployment bot's Telegram Manage Topics
  permission.

## 19. Maintenance and change policy

1. Update this document before or with a change to observable product behavior,
   trust boundaries, status classification, or acceptance criteria.
2. Update `docs/status/PROJECT_STATUS.md` when evidence changes a capability's
   current state.
3. Record consequential durable choices under `docs/decisions/`; do not rewrite
   accepted rationale invisibly.
4. Preserve backward compatibility for persisted state through explicit schema
   migrations and backups.
5. Prefer official provider interfaces and capability probes. Pin or test fast-
   moving optional dependencies; retain a simpler official fallback where
   feasible.
6. Prefer deterministic small adapters, bounded state, and reversible failure
   over deep CLI coupling, TUI scraping, autonomous repair, or speculative
   abstraction.
7. Store live deployment evidence outside Git and publish only reusable
   acceptance requirements or anonymized aggregate results.

## 20. Provenance

This baseline is derived from current repository behavior and automated tests.
Raw conversations, rollout logs, local configuration, and deployment identities
remain private operator state and must not be copied into Git.
