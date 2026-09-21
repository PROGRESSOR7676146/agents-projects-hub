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

1. Define observable behavior, trust boundaries, and acceptance criteria once,
   in the owning product requirement module. Update only the affected contract
   and its reviewed section digest; the manifest is a generated integrity guard,
   never a second normative source. A fix restoring an existing contract does
   not require rewriting requirements.
2. Update `docs/status/PROJECT_STATUS.md` only when evidence changes a capability's
   lifecycle or acceptance state. Use a short status and a contract link rather
   than repeating its behavior. Runbooks own executable procedures; the testing
   guide owns check commands and evidence boundaries. Update them only when
   those procedures change, linking to the product contract for behavior.
3. Record new consequential durable choices under `docs/decisions/`; do not
   rewrite accepted rationale invisibly or add an ADR for a mechanical fix.
   Changelog entries are short release-facing summaries and links, not another
   specification. There is no requirement to touch all these documents per change.
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
8. Background monitors, daemons, healthchecks, and scheduled timers MUST NEVER
   make live inference calls to LLMs or trigger `--live` probes. Telemetry,
   quota status, and health metrics must always be read passively from local
   cached files (`quota-cache.json`, logs) or response headers. Live probes are
   strictly prohibited except upon explicit, interactive operator request.
9. Focused development checks are partial offline feedback, never acceptance.
   Publication and CI MUST run the full canonical validator. Cheap static and
   documentation contracts MUST fail before whole-project typing and the full
   test suite. With the installed pre-push hook, its clean-commit canonical run
   is the final local gate; do not also run it manually on the unchanged tree.
   CI MUST independently execute the full gate for its exact checked-out revision.
10. An architecture review and an explicit extraction plan are required when at
    least one review trigger fires: a file of roughly more than 1,500 lines
    receives a new responsibility; a function of roughly more than 200 lines
    receives a materially new branch or lifecycle behavior; or one lifecycle or
    invariant is duplicated across multiple runtime paths. These values are
    review triggers, not blocking line-count limits or automatic refactoring
    orders.
11. When extraction is not currently justified, the bounded exception MUST name
    the concrete rationale, owner, scope, next review date, and event that
    reopens the decision.
12. A feature PR that touches an already triggered hotspot MUST either reduce
    the component's responsibility or include the bounded exception defined
    above. Mechanical splitting solely to pass a size or complexity metric is
    prohibited.
13. A PR that materially changes Controller, state, or worker lifecycle behavior
    MUST identify the component owner, transaction owner, provider-invocation
    boundary, replay safety, failure certainty, cleanup owner, focused tests,
    and exact publication evidence.
14. Architecture fitness review MUST check import direction, preserve one owner
    of each SQLite transaction, and prevent runtime code from bypassing the
    applicable state or domain facade. Orchestration and facades MAY depend on
    dependency-neutral contracts and domain helpers; lower layers MUST NOT
    import Controller, provider-worker, or Telegram runtime orchestration.
    Size and complexity measurements are for trend reporting and review
    selection, not a standalone blocking quality gate.
15. Before a major feature wave, review active hotspots, accepted bounded
    exceptions, unclosed accepted plans, and debt in worktrees, branches, and
    release tags. Resolve or explicitly carry each item before opening the wave.
16. Every accepted plan MUST record its current state, last verified revision,
    next trigger, and a closure section when the work is complete.
17. Every long-lived worktree MUST record its owner, purpose, base branch or
    revision, and a post-merge review point. Never remove a worktree
    automatically before checking its tracked, staged, and untracked state.

## 20. Provenance

This baseline is derived from current repository behavior and automated tests.
Raw conversations, rollout logs, local configuration, and deployment identities
remain private operator state and must not be copied into Git.
