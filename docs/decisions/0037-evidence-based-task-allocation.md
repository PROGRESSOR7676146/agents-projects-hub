# ADR 0037: Evidence-based task allocation as a product foundation

Status: accepted product direction; implementation design and acceptance pending
Date: 2026-09-27

## Context

Multi-provider collaboration needs more than manually assigned roles. The
system should learn which participant configurations suit which work, and
account for the resources available when a task is assigned. Retrofitting this
after implementation would lose evidence about input conditions, unsuccessful
attempts, review, rework and delegated cost.

A universal leaderboard would reward easy task selection and hide uncertainty.
A model's preference or confidence is not proof of correctness, and a cached
quota percentage is not a fungible token balance. Extra evaluation can cost
more than the allocation improvement it produces.

## Decision

Accept evaluation and resource-aware task allocation as a foundational product
capability, owned by
[the evaluation requirements](../product/EVALUATION_AND_ALLOCATION.md).
Keep observable evidence, assessments, profiles, resource state and allocation
decisions distinguishable. Enforcement and accounting remain in deterministic
Hub code; learned estimators and optional judges supply evidence and ranking.

The direction includes bounded comparative trials, but does not authorize any
live experiment now. Current routing and passive-monitor behavior remain in
force until an explicitly enabled task-allocation workflow is implemented and
accepted. Detailed team execution and context changes still require their own
review; this decision does not implicitly accept every collaboration proposal.

Evaluator choice is replaceable. Jev is a research candidate for narrow typed
judgments, not an obligatory control plane, security authority, calculator or
complete judge of software correctness. No particular learning algorithm,
exploration rate, scoring weight or provider integration is selected here.

## Consequences

Future task orchestration must be designed with evaluable outcome and cost
provenance from the start. Useful passive collection and recommendation can
precede automatic allocation. Allocation quality must be demonstrated after
including its own measurement, judging and exploration costs.

There is no runtime or state-schema change in this decision. Implementation
must retain the existing project isolation, permission, writer and recovery
boundaries. The product direction is accepted; performance claims and
deployment readiness remain unproven.
