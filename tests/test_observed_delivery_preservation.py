"""Late exact provider proof cannot erase an uncertain multipart Telegram send."""

from __future__ import annotations

import hashlib
import unittest
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.artifacts import artifact_spool_root
from hermes_codex_router.codex_appserver import (
    CodexTurnError,
    RpcError,
    StoredTurnOutcome,
    TurnResult,
)
from hermes_codex_router.state import StateError
from hermes_codex_router.turn_observation import TurnObservation
from tests import test_codex_worker as fixtures
from tests.delivery_fixture import complete_final_delivery


class DeliveryPreservationTests(unittest.TestCase):
    def test_receipt_between_delivery_read_and_commit_keeps_recovered_artifact(self) -> None:
        fixture = fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        class Client(fixtures.WorkerClient):
            observed = "unknown"

            def wait_for_turn(self, _turn_id: str) -> TurnResult:
                raise CodexTurnError(RpcError("fictional lost stream"), "Partial")

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome(
                    cast(Any, self.observed),
                    TurnResult("Exact recovered result", None, None)
                    if self.observed == "completed"
                    else None,
                )

        job_id = fixture.enqueue(1, "Example task")
        client = Client()
        worker = fixture.worker(client)
        self.addCleanup(worker.close)
        worker.run_cycle()
        state = worker.state
        root = fixture.registry.projects[0].root
        staging = root / ".hub" / "staging" / job_id
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "example.md").write_bytes(b"Example recovered artifact")
        old = state.lease_telegram_outbox("codex", "example-sender")
        assert old is not None and old.lease_token is not None
        state.delivery.begin_outbox_send(old.outbox_id, old.lease_token, 1)
        observation = TurnObservation(state, fixture.config)
        original = observation._commit_terminal

        def concurrent_receipt(*args: Any, **kwargs: Any) -> bool:
            state.delivery.mark_outbox_delivered(
                old.outbox_id, cast(str, old.lease_token), telegram_message_id=71, part_index=1
            )
            return original(*args, **kwargs)

        client.observed = "completed"
        with patch.object(observation, "_commit_terminal", side_effect=concurrent_receipt):
            self.assertTrue(observation.run_once(cast(Any, lambda: client)))
        recovered = state.get_telegram_outbox_for_job(job_id)
        self.assertNotEqual(recovered.outbox_id, old.outbox_id)
        self.assertEqual(recovered.status, "pending")
        parts = state.get_telegram_outbox_parts(recovered.outbox_id)
        documents = [part for part in parts if part.part_type == "document"]
        self.assertEqual(len(documents), 1)
        from pathlib import Path

        self.assertEqual(
            Path(cast(str, documents[0].file_path)).read_bytes(), b"Example recovered artifact"
        )
        self.assertEqual(state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(client.turns, 1)

    def test_late_terminality_preserves_unknown_and_attempted_delivery_and_spool(self) -> None:
        for status in ("completed", "failed", "interrupted"):
            for parked in (False, True):
                with self.subTest(status=status, parked=parked):
                    fixture = fixtures.CodexQueueWorkerTests()
                    fixture.setUp()

                    class Client(fixtures.WorkerClient):
                        observed = "unknown"

                        def wait_for_turn(self, _turn_id: str) -> TurnResult:
                            raise CodexTurnError(RpcError("fictional lost stream"), "Partial")

                        def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                            return StoredTurnOutcome(
                                cast(Any, self.observed),
                                TurnResult("Exact recovered result", None, None)
                                if self.observed == "completed"
                                else None,
                            )

                    job_id = fixture.enqueue(1, "Example task")
                    client = Client()
                    worker = fixture.worker(client)
                    try:
                        worker.run_cycle()
                        state = worker.state
                        old = state.get_telegram_outbox_for_job(job_id)
                        spool = artifact_spool_root(fixture.config.state_path)
                        spool.mkdir(parents=True, exist_ok=True)
                        artifact = spool / "example.md"
                        data = b"Example saved artifact"
                        artifact.write_bytes(data)
                        with state._connection:
                            state._connection.execute(
                                """INSERT INTO telegram_outbox_parts
                                   (outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,file_sha256)
                                   VALUES (?,2,'Example artifact','document',?,'example.md',?,?)""",
                                (
                                    old.outbox_id,
                                    str(artifact),
                                    len(data),
                                    hashlib.sha256(data).hexdigest(),
                                ),
                            )
                        lease = state.lease_telegram_outbox("codex", "example-sender")
                        assert lease is not None and lease.lease_token is not None
                        complete_final_delivery(
                            state, lease.outbox_id, lease.lease_token, telegram_message_id=71
                        )
                        lease = state.lease_telegram_outbox("codex", "example-sender")
                        assert lease is not None and lease.lease_token is not None
                        state.delivery.begin_outbox_send(lease.outbox_id, lease.lease_token, 2)
                        if parked:
                            state.delivery.mark_outbox_unknown(
                                lease.outbox_id,
                                lease.lease_token,
                                2,
                                error_code="example-network-unknown",
                            )
                        before = state.get_telegram_outbox_for_job(job_id)
                        parts_before = state.get_telegram_outbox_parts(old.outbox_id)
                        client.observed = status
                        observation = TurnObservation(state, fixture.config)
                        self.assertTrue(observation.run_once(cast(Any, lambda: client)))
                        self.assertEqual(state.get_telegram_outbox_for_job(job_id), before)
                        self.assertEqual(
                            state.get_telegram_outbox_parts(old.outbox_id), parts_before
                        )
                        self.assertEqual(artifact.read_bytes(), data)
                        self.assertEqual(state.get_provider_job(job_id).status, "indeterminate")
                        proof = state._connection.execute(
                            "SELECT terminal_status FROM provider_turn_terminal_evidence WHERE job_id=?",
                            (job_id,),
                        ).fetchone()
                        self.assertEqual(proof[0], status)
                        if status == "completed":
                            saved = state._connection.execute(
                                "SELECT completed_text FROM provider_execution_checkpoints WHERE job_id=?",
                                (job_id,),
                            ).fetchone()
                            self.assertEqual(saved[0], "Exact recovered result")
                        with self.assertRaises(StateError):
                            state.get_provider_result(job_id)
                        diagnostic = state.provider_job_outcome(job_id).as_dict()
                        self.assertIsNone(diagnostic["result_delivery"])
                        self.assertEqual(
                            diagnostic["notice_delivery"]["status"],
                            "unknown" if parked else "sending",
                        )
                        self.assertFalse(observation.run_once(cast(Any, lambda: client)))
                        self.assertFalse(
                            observation.observe_topic(
                                state.get_provider_job(job_id).topic_id, cast(Any, lambda: client)
                            )
                        )
                        self.assertEqual(client.turns, 1)
                    finally:
                        worker.close()
                        fixture.tearDown()
