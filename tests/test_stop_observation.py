from __future__ import annotations

import unittest
from typing import Any, cast

import test_codex_worker as fixtures

from hermes_codex_router.codex_appserver import (
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
    TurnResult,
)
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.turn_continuation_state import TurnContinuationState
from hermes_codex_router.turn_observation import TurnObservation


class StopObservationTests(unittest.TestCase):
    def test_later_exact_proof_completes_stop_without_replaying_or_publishing_result(self) -> None:
        for status in ("completed", "failed", "interrupted", "active", "unknown"):
            with self.subTest(status=status):
                fixture = fixtures.CodexQueueWorkerTests()
                fixture.setUp()
                job_id = fixture.enqueue()

                class Client(fixtures.WorkerClient):
                    observed = "unknown"
                    request_id = ""

                    def wait_for_turn(self, _turn_id: str) -> TurnResult:
                        job = worker.state.get_provider_job(job_id)
                        self.request_id, _, _ = worker.state.request_emergency_stop(
                            topic_id=job.topic_id,
                            chat_id=job.chat_id,
                            message_id=900,
                            target_agent_id="codex",
                            prepare_notice=True,
                        )
                        raise CodexTurnError(RpcError("example transport loss"))

                    def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                        return StoredTurnOutcome(
                            cast(Any, self.observed),
                            TurnResult("Result suppressed after stop", None, None)
                            if self.observed == "completed"
                            else None,
                        )

                client = Client()
                worker = fixture.worker(client)
                try:
                    worker.run_cycle()
                    self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
                    client.observed = status
                    TurnObservation(worker.state, fixture.config).run_once(
                        cast(Any, lambda: client)
                    )
                    job = worker.state.get_provider_job(job_id)
                    self.assertEqual(job.status, "indeterminate")
                    self.assertEqual(client.turns, 1)
                    terminal = status in {"completed", "failed", "interrupted"}
                    blocker = persistent_root_blocker(
                        worker.state._connection, topic_id=job.topic_id
                    )
                    self.assertEqual(blocker is None, terminal)
                    pending = worker.state.pending_emergency_stop_for_job(job_id)
                    self.assertEqual(pending is None, terminal)
                    result_count = worker.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_job_results WHERE job_id=?", (job_id,)
                    ).fetchone()[0]
                    self.assertEqual(result_count, 0)
                    if status == "completed":
                        self.assertNotIn(
                            "Result suppressed",
                            worker.state.get_telegram_outbox_for_job(job_id).telegram_html,
                        )
                        self.assertEqual(
                            TurnContinuationState(worker.state).session_status(job.session_id)[0], 0
                        )
                finally:
                    worker.close()
                    fixture.tearDown()
