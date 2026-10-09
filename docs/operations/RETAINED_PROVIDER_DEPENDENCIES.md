# Retained provider dependencies after role withdrawal

Status: source dependency inventory; integration decision pending.
Date: 2026-10-09. Owner: Hub maintainer, sole integration writer.
Last inspected source base: `851714b5005553906b29c771fcd095de133f776b`.
Scope decision: [ADR 0065](../decisions/0065-retire-hub-lead-advisor.md).
This inventory describes local commit ancestry, not main or installed state.

## Checked source dependencies

| Candidate | Dependency and retained value |
| --- | --- |
| `feat/advisor-foundations-integration`, `1c70d5639b59cfa97829bb56deaf3f95f577cb42` | Shared namespace/mount/lookup guards plus advisor-only capsule foundations |
| `feat/advisor-pipe-sequencing`, `e563dd4ca51b5d4884227681782d4a7fce275b2e` | Dormant advisor pipe primitives; subsequent in-scope work inherits this history |
| PR #155, `a542b2054250dcd7e60ad7df0969ca7462e4054b` | In-scope Claude process observations descend from pipe sequencing |
| `feat/codex-ingress-runtime`, `c52ab719c1a5c4a671b18dc50e69b3af764d7477` | In-scope Codex ingress/control-loss integration descends from the observation candidate |
| PR #154, `cc976f6828554104b7326770f1e3a67b6e2de6ef` | Owned pipe/namespace fixtures and reusable process cleanup |
| PR #174, `cad8e8d8732bfb52aadb45e15d783785af4da84a` | Joins native corpus with PR #154; preserves shared CLI/security fixes |
| PR #176, `test/claude-pipe-exchange`, `851714b5005553906b29c771fcd095de133f776b` | Current source base retains the above ancestors and test-only raw exchange |

`git merge-base --is-ancestor` verified the integration, sequencing, observation
and ingress candidates in this base, and sequencing → observation → ingress.
Published tests/reviews keep their exact source revisions. A custody table on
this source cannot establish that main or an installed release has these fixes.

## Integration decision before owner merge

Retiring the product feature does not remove its ancestors. Before integrating
in-scope visibility/control/parity fixes, prepare an explicit dependency choice:
retain dormant primitives and their existing CI with no runtime role wiring,
or split/rebase the needed changes while preserving every shared security fix
and regression. Do not assume an in-scope branch is free of cancelled work.

Neither choice happens automatically. Review the resulting exact candidate and
run its publication gates; inspect tracked/staged/untracked state before cleanup.
Keep existing branches, tests and evidence until an explicit owner decision.
No merge, deletion, deployment, service restart, live-state change or replay is
authorized by this inventory. Next trigger: dependency selection for the next
integration candidate. Closure remains open until that selection is reviewed.
