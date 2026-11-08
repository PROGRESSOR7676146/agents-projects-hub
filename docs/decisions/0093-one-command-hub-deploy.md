# ADR 0093: One-command Hub deploy

Status: proposed; first source slice implemented and fixture-tested

## Context

The installed Hub uses versioned paths in managed unit drop-ins and Claude
configuration. The prepared-release switcher in ADR 0089 operates on symlinks,
so it cannot by itself record and recover this set of file changes. A private
hand-adapted kit demonstrates the deployment sequence but is not a reusable
product interface.

## Decision

Provide a standalone standard-library tool, independent of the runtimes being
replaced, that discovers the installed revision, prepares a verified release,
and records a digest-bound plan and a recoverable service window. Private
installation information and deployment evidence remain outside the repository.
The window must back up state, verify rollback compatibility, preserve untouched
services, and verify convergence. An interrupted window requires explicit
rollback; recovery never automatically restores a database backup. A monitor
failure after retry has a distinct incomplete outcome.

The published release metadata contains `schema_max`; the current installed
`release-info` command does not. Schema compatibility must therefore be bound
to independently verified artifacts, not inferred from that CLI's output.

## Deferred and status

Migration to symlinks and `stack.py`, state migrations and the separate Codex
server start procedure are out of scope. Independent source review, release
provenance and installed acceptance remain prerequisites to calling the tool
supported. The CLI's `plan`
path validates the private input, discovers the old runtime, verifies both
releases against the live database schema, prepares or verifies an inactive
runtime, validates candidate configurations and records a digest-bound private
plan. This path has offline fixture coverage; installed behavior is not yet
accepted. The deployment window, status and explicit recovery are implemented
and exercised with offline service-manager and runtime fixtures. They are not
installed or live acceptance evidence.
