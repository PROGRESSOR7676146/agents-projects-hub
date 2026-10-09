# Retained provider dependencies after role withdrawal

Status: retain dormant primitives/tests proposed for owner integration;
candidate publication pending.
Date: 2026-10-09. Owner: Hub maintainer, sole integration writer.
Last inspected source base: `851714b5005553906b29c771fcd095de133f776b`.
Production reachability rechecked at `4483dfd9364c01cd235aded784832ccbb490538c`,
with unchanged retained source code.
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

## Proposed source integration

The integration maintainer prepares a candidate retaining dormant primitives
and existing tests/CI, with no runtime role wiring. Owner integration is pending.
Independent Astra source review found
that the five `review_materials`/`review_bridge_*` modules have no production
callers outside that set, no CLI/config/schema/worker entry point and no wired
inference client. Shared namespace/pin code remains an active dependency of
protected Claude file tools; imports do not run back toward retired modules.
Splitting this ancestry adds regression risk without removing active product
behavior. Packaging retains the dormant modules and their maintenance cost.

The [source regression](../../tests/test_retired_hub_roles.py) scans all Python
modules under `src/hermes_codex_router`, including nested packages and
conditional/function-local imports. It rejects static imports into the retired set from outside it;
absolute, relative, aliased and package-member forms have negative fixtures.
Internal dormant edges and ordinary namespace consumers remain allowed. The
guard runs through normal test discovery and existing publication/CI gates.
It prevents accidental static imports within that package. Packaging entry-point
registrations, wildcard exports, dynamic imports, copied implementations and
runtime access are outside its coverage; it is not a security sandbox. New role
orchestration still requires a new owner product decision under ADR 0065.

Focused command: `python -m unittest tests.test_retired_hub_roles`.
Review the exact resulting candidate and run publication gates. Keep existing
branches/tests/evidence; inspect tracked/staged/untracked before cleanup. No merge,
deletion, deployment, restart, live-state change or replay is authorized here.

## Closure

The dependency recommendation is complete at source-design level; retaining
packaged dormant modules still needs owner integration. Publication of the
guard is pending; ordinary custody and standalone Claude parity remain open.
Next trigger: the smallest enforceable custody candidate and separate native
human-approval acceptance in [the next session](NEXT_DEVELOPMENT_SESSION.md).
