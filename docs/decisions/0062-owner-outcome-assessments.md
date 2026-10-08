# ADR 0062: Append-only owner outcome assessments

Status: accepted design; source implementation under validation.
Date: 2026-10-08.
Source owner: Hub maintainer. Product decision owner: repository owner.

## Context and decision

The existing exact-job diagnostic distinguishes result, delivery, execution,
model and timing evidence, but has no human acceptance authority. The initial
collaboration milestone needs a minimal human journal without scores, model
judges or automatic rework. Implement the bounded owner slice of
[REQ-EVAL-010](../product/EVALUATION_AND_ALLOCATION.md) through a reserved
`/assess` command and schema45, rather than interpreting ordinary Replies.

`assessment_inputs` owns full-input fingerprinting and bounded command parsing;
`controller_assessment` owns endpoint policy and the transport-neutral input
mapping. Controller intercepts authorized non-forwarded commands before session,
material, retry and addressing work. Forward precedence stays unchanged.
Unsupported controls use the existing claim-once best-effort refusal; a provider
group observer never claims the central input. Private and legacy direct endpoints
reserve the command before their free-text or productive fallback.

HubState alone owns the immediate transaction. `outcome_assessment_state` owns
target resolution, whole-result receipt eligibility, revision validation and
append-only applied/refused records. A correction references the last applied
human command. The result's unique revision and predecessor indexes serialize
competing decisions; `revision IS NOT NULL` closes SQLite's nullable CHECK case.
Deduplication compares the complete assessment-input digest before parsing or
consulting the generic observed-message receipt. The orchestrator includes a
canonical original-message digest before quote/material normalization; `update_id`
is excluded. Raw nested replies/material metadata are not persisted. Even
semantically irrelevant source differences may conservatively conflict, never
reparse or replace a disposition. Conflict never becomes a productive request.

The existing notice table gains an independent assessment subject. An assessment
notice cannot borrow a job/stop subject; its destination must match the persisted
disposition, including a refusal for a topic that has no row. No new sender is
introduced. The existing fence, proven-rejection retry and unknown transport
rules remain independent of the assessment's authority. An unknown or lost bot
acknowledgement cannot erase the decision or become a correction target.

Disposition, input receipt, notice and command batch boundary commit together.
`ProviderJobsStateFacade.flush_batch_in_transaction` reuses the existing SQL
predicate on the caller-owned transaction; normal `flush_batch` establishes an
immediate transaction before delegation. It advances all queued future deadlines
in that topic, preserving status, holds and every execution eligibility check.
Only a new disposition closes the boundary; repeats never flush a later batch.
The assessed result and its execution/delivery evidence are unchanged.

The passive single-statement outcome projection joins only the latest applied
revision and preserves every unknown model, usage and timing field. Human reasons
are owner data, not instructions. There is no provider invocation, role workflow,
writer transfer, inference, automatic retry, scoring or judge in this slice.

## Migration, verification and limits

Migration45 creates the append-only table, rebuilds all existing notice rows and
their legacy stop-link foreign keys, then recreates delivery indexes inside the
existing migration transaction and backup discipline. Faults roll back in place;
no restored historical backup overwrites concurrent state.

Focused tests cover strict whole-final provenance, document parts, historical
generations, invalid targets, controls/quotes/materials, duplicate/conflicting
inputs, two-connection first/correction races, notice/commit/batch faults,
destination/subject isolation, all notice statuses and populated44-to45 rollback.
Independent exact-candidate review and canonical/hosted gates remain required.
Publication does not authorize deployment or establish OS custody: an unconfined
same-UID model can bypass SQL authority through state access. Independently prove
custody before activation, and run separately authorized Telegram acceptance.
