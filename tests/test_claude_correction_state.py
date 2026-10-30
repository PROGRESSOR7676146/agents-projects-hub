from __future__ import annotations

import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.claude_correction_state import ClaudeCorrectionState
from hermes_codex_router.claude_corrections import ClaudeInputFrame
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.provider_queue_capacity import QueueCapacityConfig, read_queue_capacity
from hermes_codex_router.state import StateError
from tests import test_claude_native_worker as fixtures


class ClaudeCorrectionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeNativeWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.parent_id = self.fixture.enqueue(1)
        self.worker = self.fixture.worker(None)  # type: ignore[arg-type]
        self.state = self.worker.state
        parent = self.state.lease_provider_job("claude", "example-worker")
        assert parent and parent.lease_token
        self.token = parent.lease_token
        self.state.mark_provider_job_executing(parent.job_id, self.token)
        self.journal = ExecutionJournal(self.state)
        self.binding = self.journal.prepare_claude_session(
            parent.job_id, self.token, self.fixture.root
        )
        self.store = ClaudeCorrectionState(self.state._connection)
        self.initial = ClaudeInputFrame(
            str(uuid.uuid4()), self.binding.session_id, "Example initial"
        )
        with self.state._immediate_transaction():
            self.store.prepare(self.parent_id, self.token, self.initial, str(self.fixture.root))
        self.child_id = self.fixture.enqueue(2)

    def claim(self):
        with self.state._immediate_transaction():
            return self.store.claim(self.parent_id, self.token)

    def test_submission_is_committed_before_caller_can_write_and_claims_once(self) -> None:
        frame = self.claim()
        assert frame
        self.assertEqual(self.state.get_provider_job(self.child_id).status, "executing")
        self.assertIsNone(self.claim())
        self.assertEqual(self.state.get_provider_job(self.child_id).attempt_count, 1)
        saved_child = self.store.child(self.parent_id)
        assert saved_child is not None
        self.assertEqual(saved_child["input_uuid"], frame.uuid)
        capacity = read_queue_capacity(
            self.state._connection, QueueCapacityConfig(2), now=datetime.now(timezone.utc)
        )
        self.assertEqual(capacity.busy_agents, {"claude": 1})
        self.assertEqual(capacity.occupied_roots, 1)

    def test_correction_size_is_checked_before_reservation(self) -> None:
        for size in (20_000, 20_001):
            with self.subTest(size=size):
                self.setUp()
                if size == 20_001:
                    # The released provider_jobs CHECK already rejects this
                    # input before admission, including batched payloads.
                    with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
                        self.state._connection.execute(
                            "UPDATE provider_jobs SET payload_text=? WHERE job_id=?",
                            ("x" * size, self.child_id),
                        )
                    self.assertIsNone(self.store.child(self.parent_id))
                    self.assertEqual(self.state.get_provider_job(self.child_id).status, "queued")
                    continue
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE provider_jobs SET payload_text=? WHERE job_id=?",
                        ("x" * size, self.child_id),
                    )
                frame = self.claim()
                self.assertEqual(frame is not None, size == 20_000)
                child = self.state.get_provider_job(self.child_id)
                self.assertEqual(child.status, "executing" if size == 20_000 else "queued")
                self.assertEqual(child.attempt_count, 1 if size == 20_000 else 0)

    def test_model_selection_change_seals_admission_but_not_original_receipt_or_result(
        self,
    ) -> None:
        with self.state._connection:
            self.state._connection.execute("UPDATE agent_sessions SET model='other-model'")
        self.assertIsNone(self.claim())
        with self.state._immediate_transaction():
            self.store.receipt(self.parent_id, self.token, self.initial.uuid, 1)
            self.store.result(self.parent_id, self.token, 1)
        self.journal.record_claude_completion(
            self.parent_id,
            self.token,
            self.binding.session_id,
            "Original answer",
            cwd=self.fixture.root,
            material_notice="",
        )

    def test_linkage_cannot_be_replaced_or_deleted(self) -> None:
        self.claim()
        for sql in (
            "INSERT OR REPLACE INTO claude_input_children SELECT * FROM claude_input_children",
            "INSERT OR REPLACE INTO claude_input_runs SELECT * FROM claude_input_runs",
            "DELETE FROM claude_input_children",
            "DELETE FROM claude_input_runs",
            "UPDATE claude_input_children SET input_uuid='example-other'",
            "UPDATE claude_input_runs SET native_session_id='example-other'",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaises(sqlite3.IntegrityError),
                self.state._connection,
            ):
                self.state._connection.execute(sql)

    def test_claim_rollback_preserves_original_queue_position(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Example commit failure"):
            with self.state._immediate_transaction():
                self.assertIsNotNone(self.store.claim(self.parent_id, self.token))
                raise RuntimeError("Example commit failure")
        self.assertEqual(self.state.get_provider_job(self.child_id).status, "queued")
        self.assertEqual(self.state.get_provider_job(self.child_id).attempt_count, 0)
        self.assertIsNone(self.store.child(self.parent_id))

    def test_uuid_collision_and_ignored_child_update_do_not_reserve_input(self) -> None:
        with patch(
            "hermes_codex_router.claude_correction_state.uuid.uuid4",
            return_value=uuid.UUID(self.initial.uuid),
        ):
            self.assertIsNone(self.claim())
        with self.state._connection:
            self.state._connection.execute(
                "CREATE TRIGGER example_skip_child BEFORE UPDATE OF status ON provider_jobs "
                "WHEN NEW.job_id='%s' BEGIN SELECT RAISE(IGNORE); END" % self.child_id
            )
        with self.assertRaises(StateError):
            self.claim()
        self.assertIsNone(self.store.child(self.parent_id))
        self.assertEqual(self.state.get_provider_job(self.child_id).status, "queued")

    def test_child_queue_notices_follow_execution_and_terminal_status(self) -> None:
        self.store = ClaudeCorrectionState(
            self.state._connection, visibility=self.state.queue_visibility
        )
        with self.state._immediate_transaction():
            self.state.queue_visibility.admitted_in_transaction(
                self.child_id, now=datetime.now(timezone.utc)
            )
        self.claim()
        notices = self.state._connection.execute(
            "SELECT kind,status FROM task_lifecycle_notices WHERE job_id=?", (self.child_id,)
        ).fetchall()
        self.assertIn(("executing", "pending"), [tuple(row) for row in notices])
        self.state.cancel_active_provider_job(self.parent_id, self.token, complete_stops=True)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM task_lifecycle_notices WHERE job_id=? AND status IN ('pending','leased')",
                (self.child_id,),
            ).fetchone()[0],
            0,
        )

    def test_bulk_stale_recovery_does_not_orphan_submitted_child(self) -> None:
        self.claim()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE status='executing'",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
        self.state.recover_stale_provider_jobs(agent_id="claude")
        self.assertEqual(self.state.get_provider_job(self.parent_id).status, "indeterminate")
        self.assertEqual(self.state.get_provider_job(self.child_id).status, "indeterminate")

    def test_stop_hold_material_context_and_changed_selection_leave_fifo(self) -> None:
        for reason in ("stop", "hold", "context", "model", "generation", "ingress"):
            with self.subTest(reason=reason):
                self.setUp()
                connection = self.state._connection
                with connection:
                    if reason == "stop":
                        self.state.request_emergency_stop(
                            topic_id=self.state.get_provider_job(self.parent_id).topic_id,
                            chat_id=self.state.get_provider_job(self.parent_id).chat_id,
                            message_id=99,
                            target_agent_id="claude",
                        )
                    elif reason == "hold":
                        connection.execute(
                            "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at,decision) VALUES(?,?,?,'pending')",
                            (self.child_id, self.parent_id, datetime.now(timezone.utc).isoformat()),
                        )
                    elif reason == "context":
                        child = self.state.get_provider_job(self.child_id)
                        watermark = self.state.record_visible_turn(
                            child.topic_id,
                            agent_id="codex",
                            provider="codex",
                            model="example-model",
                            user_excerpt="Example",
                            response_excerpt="Example answer",
                        )
                        connection.execute(
                            "UPDATE provider_jobs SET context_watermark=? WHERE job_id=?",
                            (watermark, self.child_id),
                        )
                    elif reason == "model":
                        connection.execute(
                            "UPDATE provider_jobs SET model='other-model' WHERE job_id=?",
                            (self.child_id,),
                        )
                    elif reason == "generation":
                        connection.execute(
                            "UPDATE agent_sessions SET generation=generation+1 WHERE session_id=?",
                            (self.state.get_provider_job(self.parent_id).session_id,),
                        )
                    else:
                        # Fresh known provenance cannot mix with an unknown parent.
                        child = self.state.get_provider_job(self.child_id)
                        self.state.cancel_provider_job(self.child_id)
                        self.child_id = self.state.enqueue_provider_job(
                            idempotency_key="example-known-ingress",
                            chat_id=child.chat_id,
                            message_id=3,
                            topic_id=child.topic_id,
                            agent_id=child.agent_id,
                            session_id=child.session_id,
                            session_generation=child.session_generation,
                            model=child.model,
                            effort=child.effort,
                            payload_text="Example known ingress",
                            telegram_ingress_identity="hub",
                        )[0].job_id
                self.assertIsNone(self.claim())
                self.assertNotEqual(self.state.get_provider_job(self.child_id).status, "executing")

    def test_receipts_without_result_cannot_complete_or_requeue(self) -> None:
        frame = self.claim()
        assert frame
        with self.state._immediate_transaction():
            self.store.receipt(self.parent_id, self.token, self.initial.uuid, 1)
            self.store.receipt(self.parent_id, self.token, frame.uuid, 1)
            with self.assertRaises(StateError):
                self.store.validate_completion(self.parent_id)
        with self.assertRaises(StateError):
            self.state.reject_unaccepted_steer(
                self.child_id, self.state.get_provider_job(self.child_id).lease_token or ""
            )

    def test_saved_completion_recovers_child_atomically_with_parent_result(self) -> None:
        frame = self.claim()
        assert frame
        with self.state._immediate_transaction():
            self.store.receipt(self.parent_id, self.token, self.initial.uuid, 1)
            self.store.result(self.parent_id, self.token, 1)
            self.store.receipt(self.parent_id, self.token, frame.uuid, 2)
            self.store.result(self.parent_id, self.token, 2)
        self.journal.record_claude_completion(
            self.parent_id,
            self.token,
            self.binding.session_id,
            "Example complete",
            cwd=self.fixture.root,
            material_notice="",
        )
        self.assertEqual(self.state.get_provider_job(self.child_id).status, "executing")
        result = self.state.commit_provider_result(
            self.parent_id,
            self.token,
            visible_response="Example complete",
            sender_agent_id="claude",
            telegram_html="Example complete",
            provider_session_id=self.binding.session_id,
        )
        self.assertIsNotNone(result)
        self.assertEqual(self.state.get_provider_job(self.child_id).status, "completed")
        self.assertIsNone(self.journal.read(self.child_id))
        saved_child = self.store.child(self.parent_id)
        assert saved_child is not None
        self.assertEqual(saved_child["receipt_terminal"], 2)

    def test_parent_lease_heartbeat_covers_child_and_stale_claim_selects_parent_only(self) -> None:
        self.claim()
        self.state.heartbeat_provider_job(self.parent_id, self.token, lease_seconds=120)
        self.assertEqual(
            self.state.get_provider_job(self.parent_id).lease_expires_at,
            self.state.get_provider_job(self.child_id).lease_expires_at,
        )
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE status='executing'",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
        recovered = self.journal.claim_stale("claude", "example-recovery")
        assert recovered
        self.assertEqual(recovered.job_id, self.parent_id)
        self.assertIsNone(self.journal.claim_stale("claude", "example-recovery"))

    def test_confirmed_stop_cancels_child_but_unknown_parent_preserves_child_uncertainty(
        self,
    ) -> None:
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                self.setUp()
                self.claim()
                if stopped:
                    self.state.cancel_active_provider_job(
                        self.parent_id, self.token, complete_stops=True
                    )
                else:
                    self.state.terminate_provider_job_with_notice(
                        self.parent_id,
                        self.token,
                        status="indeterminate",
                        error_class="ambiguous_execution",
                        error_code="example_fault",
                        sender_agent_id="claude",
                        telegram_html="Unconfirmed",
                    )
                self.assertEqual(
                    self.state.get_provider_job(self.child_id).status,
                    "cancelled" if stopped else "indeterminate",
                )


if __name__ == "__main__":
    unittest.main()
