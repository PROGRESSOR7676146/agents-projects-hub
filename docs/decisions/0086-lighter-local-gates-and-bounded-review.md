# ADR 0086: Lighter local gates and bounded review

Status: accepted
Date: 2026-10-30
Owner: repository owner
Supersedes: the full-suite part of [ADR 0042](0042-pre-commit-gate.md)

## Context

ADR 0042 ran the whole test suite before every commit because it took about a
minute. It now takes 7 to 25 minutes on the owner's machine, which also runs
the Hub. Under that load some test modules time out at random. A one-file
documentation commit was refused three times in a row by modules it could not
affect. Each pushed change then runs the full suite twice more: once in the
pre-push hook and once in CI.

Independent review had no stopping rule. One credential-boundary change went
through five review rounds. The last verdict asked for more evidence, not for
a fix, and the owner accepted the change as it was.

## Decision

**Commit gate.** A commit runs these checks:

- every cheap contract;
- the privacy/history scan;
- the test modules that the change can affect.

A test module is affected when it changed, or when it names a changed source
module or script. A documentation-only commit runs no tests. Anything else
runs the full suite. This covers dependencies, fixtures, hooks, workflows, any
`__init__.py`, and a repository without `HEAD`.

The pre-push canonical run and CI keep the full suite and whole-project typing,
unchanged. A broken test that the commit gate misses therefore still blocks
publication.

**Review rounds.** One full independent review is run. A second round is run
only when the first one found a blocker, meaning a demonstrated defect with a
failure scenario, and it checks only those blockers. After that, the open
findings are recorded in the pull request and the owner decides. A request for
more evidence never opens another round.

## Consequences

- A local commit costs seconds for documentation and minutes for focused code
  changes.
- A cross-module regression is caught at push or in CI, not at commit. Local
  history can then hold a commit that would have failed the full suite. Push
  and CI still keep it out of `main`.
- A review cannot block work indefinitely. The owner sees what is still open
  and decides.
