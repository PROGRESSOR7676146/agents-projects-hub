# Maintenance requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#maintenance).

## 18. Known limitations

Live acceptance is point-in-time and needs repetition after material changes.
Provider resume capabilities vary; OpenCode/Antigravity lack semantic tlive parity,
Hermes local transfer needs verification, and Antigravity rotation awaits support.

Recovery covers existing-machine service failure; application restore is planned
and host recovery externally owned. In-flight turns are not recoverable.

Extra Codex/Claude workers require explicit rollout; OpenCode/Antigravity have one
slot each. Claude tools/routes retain separate installed/live gates; hostile
same-UID isolation is not claimed, and unsupported managed local/steering stays
unsupported. Topic creation requires Telegram Manage Topics permission.

## 19. Maintenance and change policy

1. Observable outcomes, boundaries and acceptance MUST have one owning module; update only affected
    contracts/digests, not requirements for a fix restoring them.
2. Status MUST change only with lifecycle/acceptance evidence; runbooks own procedures and testing
    guides own checks, with contract links instead of duplication.
3. Consequential choices MUST retain visible ADR rationale; mechanical fixes need no ADR, and
    changelogs remain short release summaries.
4. Persisted-state compatibility MUST use explicit migrations and backups.
5. Prefer official interfaces/probes, tested or pinned fast-moving dependencies and simple official
    fallbacks.
6. Prefer small deterministic adapters, bounded state and reversible failure over scraping,
    autonomous repair or speculative abstraction.
7. Live deployment evidence MUST stay outside Git; publish only reusable criteria or anonymized
    aggregates.
8. Background monitoring, daemons, health checks and timers MUST NEVER invoke inference or live
    probes; telemetry MUST be passive and live probes require explicit interactive owner requests.
9. Focused checks MUST NOT claim acceptance; publication/CI MUST run the canonical gate for the
    exact revision, with cheap checks first and no redundant unchanged-tree local run.
10. New responsibility in large files/functions or duplicated lifecycle invariants MUST trigger
    architecture review and an extraction plan; size is a review trigger, not an automatic refactor.
11. A bounded deferral MUST name rationale, owner, scope, next review date and reopening event.
12. Feature changes in triggered hotspots MUST reduce responsibility or carry that exception;
    recorded hotspot growth MUST fail the gate without reviewed bounds, and metric-only splitting is
    prohibited.
13. Material Controller/state/worker changes MUST name ownership, transaction/invocation boundaries,
    replay/failure/cleanup behavior and focused/publication evidence.
14. Architecture review MUST preserve import direction, one SQLite transaction owner and facade
    access; lower layers MUST NOT import runtime orchestration, and metrics beyond the growth rule
    remain review evidence.
15. Before major feature work, hotspots, exceptions, unfinished plans and worktree/branch/tag debt
    MUST be resolved or explicitly carried.
16. Accepted plans MUST record state, verified revision, next trigger and closure.
17. Long-lived worktrees MUST record owner, purpose, base and post-merge review; automatic removal
    MUST first inspect tracked, staged and untracked state.

## 20. Provenance

This baseline derives from repository behavior and automated tests.
Conversations, rollout logs, local configuration and deployment identities MUST
remain private and outside Git.
