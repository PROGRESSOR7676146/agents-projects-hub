# Quality and stability review

Date: 2026-09-11
Scope: analysis and offline reproductions; no runtime implementation or deployment

This is the initial audit record. Subsequent implementation status and the
current checkpoint are maintained in the [reliability plan](RELIABILITY_PLAN.md).
The findings below describe the audited baseline, not all later working changes.

The highest product risk is useful provider work without a recoverable visible
outcome. The queue correctly avoids replaying uncertain side effects, but that
safety property does not by itself preserve partial results or recover delivery.

## Confirmed findings and proposed acceptance

| Priority | Finding and source | Proposed correction and acceptance |
| --- | --- | --- |
| P0 release | The canonical validation command fails the privacy scan in the current tree and reachable history. | Replace private fixtures with fictional data and reconcile affected history through a separately authorized Git operation. A later deletion commit alone cannot pass the history gate. Do not publish the affected revision. |
| P1 outcome | `CodexAppServerClient._request` buffers notifications, but `wait_for_turn` never consumes that buffer. A fictional completed item and turn arriving before the `turn/start` response are ignored by the waiter. | Use one ordered event dispatcher and consume already-received matching events before waiting for more. Test completion before, between, and after RPC responses; the provider must run once. Whether the live provider emits this ordering remains unproven. |
| P1 outcome | `wait_for_turn` retains visible messages only in a local list and raises a plain `RpcError` on terminal failure. Previously collected text is unavailable to the caller. | Persist bounded visible item IDs, phase, and text as they complete; retain partial progress with an explicit incomplete status after failure. Never publish reasoning or tool output. Test visible text followed by failure, disconnect, and abrupt worker death. |
| P1 recovery | `ExternalQueueWorker._execute` records `executing` before setup. A simulated `start_thread` failure with zero `start_turn` calls becomes `indeterminate`. New thread identity is bound only by successful result commit, and the main accepted turn ID is not recorded durably. | Distinguish preparation, possible invocation, known acceptance, provider terminal state, and result delivery. Persist newly created thread and accepted turn identity at their boundaries without rewriting the enqueue snapshot. Retry only with positive proof of non-execution. |
| P1 recovery | The generic Codex error branch converts different failure causes into the same uncertainty notice. There is no implemented result-reconciliation path in this worker. | Preserve bounded structured error class independently from side-effect certainty. A provider rate limit after tool work can be a known cause with uncertain task effects. Recover a completed result by exact turn ID through a supported read-only interface before considering an explicit continuation. |
| P1 delivery | Both sender paths pass `delay_seconds=1` even though `TelegramError` retains `retry_after`. Twenty simulated 429 responses exhaust the outbox within 19 simulated seconds despite a 60-second server instruction. | Persist a retry deadline respecting the server minimum, bounded backoff and jitter. Test restart during cooldown and delivery after recovery with no new provider turn. Distinguish permanent rejection from transient failure. |
| P2 transport | `StdioJsonLineTransport.receive` ignores its timeout; blocking `readline` can hold the sole provider execution slot indefinitely. WebSocket clean closure does not explicitly enqueue an EOF sentinel. | Enforce deadlines at real transport boundaries and propagate closure immediately. Test a silent child and graceful/abrupt connection closure. This is a source-confirmed risk; a production hang was not reproduced in this review. |
| P2 maintenance | `service.py` has 2,387 lines and `handle_update` has 679; `state.py` has 3,289 lines. Compatibility and isolated execution retain duplicate outcome/delivery logic. | Extract lifecycle and delivery policy behind existing tests in small changes. Retire compatibility modes only after their documented acceptance and rollback window. File size indicates change risk, not a defect by itself. |

## Validation and documentation debt

At the source baseline below, `.venv/bin/python scripts/validate.py` stops at
privacy. Separate diagnostic stages, not a replacement green gate, produced:

- Unit/integration discovery: 506 tests, OK, 3 socket tests skipped by sandbox.
- Ruff lint and Pyright: passed; example registry validation: passed.
- Ruff formatting: two pre-existing unformatted files.
- Documentation contract: pre-existing section 19 content-hash mismatch.
- Release metadata: passed with missing-tag debt.
- Five fictional offline scenarios reproduced the event-ordering, partial-text,
  setup classification, missing session binding, and retry-after findings.

Passing aggregate tests does not cover untested event permutations. Convert the
reproductions into regression tests before implementing fixes. Preserve the
existing subprocess fault matrix and extend it across durable visible-item and
accepted-turn boundaries.

The acceptance text `AC-F-003` still requires automatic unseen-context injection,
contradicting `REQ-CTX-002` and ADR 0009. Reconcile acceptance with explicit context
retrieval. The stale status claim about live quota probes was corrected during
this review: the monitor passes `live=False`. This review made no live inference
health checks. Hash/link checking cannot establish semantic agreement between
requirements and acceptance criteria.

## Next implementation sequence

1. Restore the privacy, format, and documentation gates before any release.
2. Fix event buffering, preserve visible progress, and expose safe error causes.
3. Persist accepted execution identity and implement bounded result recovery.
4. Respect Telegram cooldowns and test delivery-only recovery.
5. Add passive outcome metrics: accepted requests, visible final results,
   partial failures, unresolved execution, recovered results, delivery lag, and
   oldest queued work. Count heartbeats separately from useful task progress.
6. Complete separately authorized provider/Telegram acceptance and machine-loss
   recovery drills before expanding provider breadth.

## Checkpoint and lessons

The source baseline is commit
`634b0e9c2400fd68feb709f0745454654b4ec0ab` on `release/0.7.0-rc`, verified before
these documentation edits. `AGENTS.md` was already modified and was preserved.
The local tracking reference reported one commit ahead; remote state was not
queried. This review adds documentation only and creates no commit. Exact
built/deployed identity and incident evidence are private operational context,
not repository content; repository tests do not establish deployment acceptance.

The scope authorized analysis, temporary offline fixtures, and this reusable
checkpoint. It did not authorize implementation, history rewrite, publication,
service changes, or live provider acceptance. Outstanding work is ordered above.
No review-owned service remains running; temporary fixtures clean up their state.

Two delegated lookup jobs timed out with empty results and supplied no evidence.
Findings were checked directly against source and fictional reproducers. The
reusable lesson is to require event-ordering and failure-boundary evidence,
rather than infer reliability from the number of passing tests. Existing test
helpers and project instructions suffice; no new skill or integration is needed.
