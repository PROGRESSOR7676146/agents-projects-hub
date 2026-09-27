# ADR 0045: Withdraw the provider-expansion gate

Status: accepted
Date: 2026-09-27

## Context

The requirements baseline carried product principle 10 ("Evidence before
breadth"), a non-goal forbidding new providers before the current provider set
passed live E2E, and a capability-matrix row rejecting provider expansion. The
owner had introduced them as a temporary restriction. The Claude Code worker
scaffold (ADR 0039) and the accepted Claude/Codex collaboration (ADR 0040)
contradicted them.

## Decision

On 2026-09-27 the owner withdrew the gate. Principle 10, the non-goal and the
matrix row are removed. Claude Code is listed as a provider in scope, in the
provider status table and in the capability matrix, as a repository scaffold
with live acceptance pending.

Nothing else changes. Lifecycle labels and evidence levels still decide what
may be called implemented or accepted; repository tests are still not live
acceptance; every Claude security boundary in REQ-AUTH-009 and
REQ-WRITER-013 remains. The live-acceptance backlog in the stabilization plan
stays as tracked quality debt rather than a gate.

## Consequences

Provider work, including Claude parity with Codex, may proceed in the
repository while earlier capabilities await live acceptance. The risk that
breadth outpaces acceptance is tracked as R-016 and mitigated by the backlog
and by never presenting repository evidence as deployment evidence.
