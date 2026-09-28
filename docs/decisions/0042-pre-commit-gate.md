# ADR 0042: Pre-commit gate with the full test suite

Status: accepted
Date: 2026-09-27

## Context

AGENTS.md already required a privacy/history scan before every commit, but
nothing enforced it or any test run before a commit. Only the pre-push hook
ran the canonical gate. Local commits therefore accumulated on a red suite
until the next push, and one branch carried three such commits. With
[ADR 0041](0041-parallel-isolated-test-modules.md) the full suite takes under a
minute, so running it before every commit is affordable.

## Decision

The repository-managed hook directory also receives a versioned
`pre-commit` hook, installed together with `pre-push` by
`publish_preflight --install`. Installation writes both hooks as one
complete, immutable set and activates it with a single atomic switch of the
link Git runs hooks through, so no worktree ever runs a mixed gate. The
previous set, the local settings and every per-worktree hook path stay intact
until the installation is verified; a failure returns to them, reads the
result back, and reports when the return could not be confirmed instead of
claiming it. Settings are restored before a link the installation created is
removed, so even a failed return leaves Git pointing at a complete set.

The hook refuses a commit while unstaged changes or untracked, non-ignored
files exist, because the checks read the working tree. This check uses the
index Git hands to the hook, so `commit -a` and path commits are judged by
their own index. The hook then clears repository-local Git variables, as the
pre-push hook does, and runs `validate.py --profile commit`: every cheap
contract, the privacy/history scan and the complete parallel suite.
Whole-project Pyright remains in the pre-push canonical run and CI, so a
commit costs about a minute and a half instead of three.

A checkout that predates the commit profile falls back to focused checks plus
the history scan, so older branches can still commit.

## Consequences

The mandatory pre-commit privacy scan is now automatic. A red suite can no
longer be committed without Git's explicit `--no-verify`, which remains
prohibited for routine work. Partial staging requires stashing unrelated
changes first. The pre-push canonical run still repeats the tests on the clean
commit; ADR 0034 still rejects a receipt cache that would skip it.
