# ADR 0041: Parallel isolated test modules in validation

Status: accepted
Date: 2026-09-27

## Context

The canonical gate ran the whole suite in one `unittest discover` process for
about six minutes, after roughly three minutes of Pyright. Contributors skipped
it before commits, and a branch accumulated three commits on top of a red suite.
The focused profile selected modules as `tests.test_x`, but sixteen modules
import sibling fixtures as top-level modules, as discovery allows. Those
selectors failed at import, so the documented iteration loop could not run them.

## Decision

The canonical `full tests` stage first asks `unittest` discovery, in a child
process that runs no tests, for every test module under `tests` by dotted name
(nested test packages included) and its test count. Each module then runs in
its own `python -m unittest <module>` process with the same import path,
several at a time. The default is the CPU count capped at eight; `--jobs 1..32`
overrides it. Every module finishes before failures are reported, every failing
module is named with its output (also after a timeout), a module must run
exactly the tests discovery found, a `test*.py` file that contributes no tests
fails, and each module has a 600-second deadline. The stage still runs the complete suite and still follows the cheap
contracts, history scan and Pyright.

Focused selectors keep the `tests.` form and run with `tests/` on the import
path, importing modules exactly as discovery does.

## Consequences

Local canonical tests take under a minute on eight CPUs instead of about six.
Every module must now pass alone in a fresh process, so a hidden dependency on
state created by another module fails instead of passing by import order. The
converse check is lost: pollution that only appears when two modules share a
process is no longer exercised. Parallel load exposed one such timing defect
already, a fake Git signature verifier that exited before reading its stdin.

## Alternatives

`pytest-xdist` would add a second runner, plugins and lock entries for the
same isolation. A shared-process parallel runner would keep cross-module
pollution but also its ordering dependence. Both were rejected.
