# ADR 0054: Notice-bound work retry reports

Status: proposed; repository candidate under offline validation
Date: 2026-10-05

## Context

[REQ-QUEUE-012/013](../product/DURABLE_QUEUE_AND_CONTROL.md#implemented-queue-compatibility-and-local-provider-worker-isolation)
requires active-work retries to report or join the existing work without another
invocation. An exact Reply `retry` that was not a confirmed-terminal Codex failure
notice previously reached ordinary productive admission and created another job.

## Candidate decision

Treat an exact plain Reply `retry` as a control before productive admission.
Preserve the existing inspection-first continuation for a delivered Codex failure
notice with proven terminal evidence. That continuation remains the separate
owner choice; its old task is never replayed. Unaddressed ordinary text, forwards,
materials and selected quotes retain their existing routing semantics.

For the supported Hub/external-queue/external-outbox mode, a positive durable
control-notice receipt identifies the exact existing job within the numeric
chat/topic. A report records the state at request time rather than attempting to
attach another provider writer. Changed binding and unsupported/unknown references
give a bounded refusal; they never fall through into a productive `retry` turn.
Other compatibility modes explicitly refuse this active-control surface.

Reuse the existing notice and observed-input tables. One HubState-owned immediate
transaction binds the report to the input message and original job, prepares its
immutable `retry_report` snapshot, and claims the input. No schema, extra broker,
provider call, lease release, approval answer, replay or session mutation is needed.
Only a current accepted activity binding may label a native approval wait.
An expired execution lease is unconfirmed; existing terminal evidence and owner
resolutions remain distinct from an unresolved outcome.

The sender delivers a historical snapshot even if work subsequently changes;
its text explicitly says "At retry time". Reusing a received message cannot
rewrite or enqueue the snapshot again. Existing send-start, receipt and unknown
delivery rules apply unchanged. This does not claim exactly-once Telegram sends.
Unsupported references use the existing immediate command refusal path rather
than inventing a job reference for durable delivery.

## Architecture and ownership

The lead agent owns this candidate. `controller_retry.py` extracts the existing
terminal-continuation branch and owns deterministic control selection; Controller
still owns authorization, topic observation and response transport.
`work_retry_state.py` owns report queries on the existing connection and the one
input/notice transaction. It depends on state contracts and passive binding
validation, never Controller, workers or Telegram runtime orchestration.
Worker invocation and cleanup, provider-job transaction ownership, and sender
delivery remain in their existing components.

This extraction reduces the responsibility and length of `service.py` and its
already-triggered update dispatcher. No hotspot bound is raised. The next review
point is any new productive retry branch, binding/source type, compatibility mode
or change to transaction/delivery ownership; the lead maintainer owns that review.

## Evidence and limits

Offline tests reproduce the old second-job admission, preserve exact terminal
continuation, and cover current execution, approval waiting, expired leases,
unknown outcomes and same-root exclusion, changed writer, foreign numeric scope,
restart/deduplication, transactional rollback, existing owner resolution, unknown
delivery and unchanged plain-text routing. Fake transports make no model or
Telegram calls. Focused and canonical checks must name the exact candidate.

Independent Controller/state review and hosted checks are required before readiness.
Native/Telegram acceptance and deployment require separate owner authorization.
Approval before native acceptance and other retry entry surfaces remain open.
