# Risk register

Status: active  
Last updated: 2026-09-27

| ID | Risk | Current control | Residual action/status |
| --- | --- | --- | --- |
| R-001 | Cross-project or cross-topic file access | Numeric binding, local registry, canonical allowlisted roots, immutable session binding, sandbox | Continue multi-project isolation tests and live canaries. |
| R-002 | Wrong agent answers or idle providers spend tokens | Central deterministic ingress, Reply/mention precedence, provider pollers disabled, bounded journal | Run deployment-local E2E and observe provider invocation metadata. |
| R-003 | Duplicate or concurrent turns | Idempotency receipts, one active turn/lane, writer lease | Verify offset/session continuity during live restart E2E. |
| R-004 | Secret or hidden-context disclosure | Token files, restrictive permissions, redaction, visible-only hooks/handoffs | Keep raw rollouts and environment dumps outside Git and Telegram. |
| R-005 | Optional multi-auth breaks Codex/Hub | Official stdio fallback; component health checks; manual Hermes recovery | Test controlled/natural exhaustion and version upgrades. |
| R-006 | Provider CLI upgrade breaks adapter | Structured interfaces, capability probes, visible failure | Pin/test versions and retain rollback; never screen-scrape as fallback. |
| R-007 | Hermes or tlive becomes a mandatory dependency | Independent services and alerts; Hub continues without either | Exercise independent failure recovery periodically. |
| R-008 | Machine loss destroys session continuity | Separate off-machine WSL backup and isolated cold-restore drill plan covers immutable releases, durable state, projects, provider stores, and secrets | Implement encrypted automation and complete the first timed private drill; in-flight turns remain unrecoverable. |
| R-009 | In-flight turn cannot be recovered | Fail closed; persist completed state only | Explicit accepted limitation; never promise exact recovery. |
| R-010 | Antigravity account rotation duplicates side effects or corrupts auth | Automation deferred; manual provider-aware runbook only | Wait for stable supported headless pool/idle semantics. |
| R-011 | Documentation contradicts code | Canonical PRD, status file, doc map, automated tests as implementation evidence | Update durable docs with behavior changes. |
| R-012 | Private deployment data is committed | Mandatory privacy gate, fictional fixtures, no history/handoff directories | Block validation and CI; rewrite published Git history if exposure occurs. |
| R-013 | Autonomous repair weakens approvals or hides failure | Deterministic runbooks, no auto-approval, independent channels | Reject repairs that expand authority or silently retry side effects. |
| R-014 | Agent-authored changes merge without independent review | Owner-only merge; independent agent or owner review for large or lifecycle/security PRs (AGENTS.md) | Record findings in each PR; the rule is procedural, not enforced by GitHub for a single maintainer. |
| R-015 | Survived failures leave no trace | Bounded class-and-site diagnostics in the journal; Ruff S110/S112 (ADR 0043) | Broad handlers that record elsewhere stay unflagged; exact causes need local reproduction. |
| R-016 | Repository-complete features accumulate without live acceptance | Live-acceptance backlog in the stabilization plan; evidence levels (the provider-expansion gate was withdrawn in ADR 0045) | Owner-run canary per backlog item; repository tests are not deployment evidence. |
| R-017 | Controller and state hotspots keep growing | Maintenance rules 10–14; bounded exceptions | Automated growth ratchet and extraction of `_handle_update` and configuration loading (stabilization stages 2–3). |
| R-018 | Real-clock tests fail when the host suspends or is overloaded | Parallel runner names each failing module; commit gate blocks | Convert lease and join timing in tests to injected clocks. |
| R-019 | Claude CPA failures hold canonical roots; billing route unproven | Loopback CPA preflight, text-only adapter, no replay (ADR 0039) | Owner decision on classifying side-effect-free failures; private CPA route acceptance. |
| R-020 | Rollback worktrees under `/tmp` disappear | Rollback branches are also on the remote | Move them to a persistent location with the owner's approval. |
