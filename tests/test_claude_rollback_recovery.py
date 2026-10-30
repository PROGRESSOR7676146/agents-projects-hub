"""Retained schema55 work under the previous single-input invocation runtime."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.claude_recovery import recover_claude_completion
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ExternalCliAdapter
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState
from tests import test_claude_correction_state as fixtures


class ClaudeRollbackRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeCorrectionStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.parent = self.fixture.parent_id
        self.child = self.fixture.child_id
        self.frame = self.fixture.claim()
        assert self.frame is not None
        self.tail = self.fixture.fixture.enqueue(3)
        self.enterContext(
            patch.object(ExternalCliAdapter, "run_turn", side_effect=AssertionError("no replay"))
        )

    def expire(self) -> None:
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE status='executing'",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )

    def save_completion(self) -> str:
        fixture = self.fixture
        assert self.frame is not None
        with self.state._immediate_transaction():
            fixture.store.receipt(self.parent, fixture.token, fixture.initial.uuid, 1)
            fixture.store.result(self.parent, fixture.token, 1)
            fixture.store.receipt(self.parent, fixture.token, self.frame.uuid, 2)
            fixture.store.result(self.parent, fixture.token, 2)
        text = ("Saved parent and correction result. " * 300).strip()
        fixture.journal.record_claude_completion(
            self.parent,
            fixture.token,
            fixture.binding.session_id,
            text,
            cwd=fixture.fixture.root,
            material_notice="",
        )
        return text

    def test_reserved_unreceipted_child_retains_uncertainty_and_never_becomes_fifo_work(
        self,
    ) -> None:
        child = self.fixture.store.child(self.parent)
        assert child is not None
        child_before = tuple(child)
        self.expire()
        self.assertTrue(self.fixture.worker.run_cycle())
        for job_id in (self.parent, self.child):
            self.assertEqual(self.state.get_provider_job(job_id).status, "indeterminate")
            self.assertEqual(self.state.get_provider_job(job_id).attempt_count, 1)
        self.assertIsNotNone(
            persistent_root_blocker(
                self.state._connection, topic_id=self.state.get_provider_job(self.parent).topic_id
            )
        )
        child_after = self.fixture.store.child(self.parent)
        assert child_after is not None
        self.assertEqual(tuple(child_after), child_before)
        self.assertIsNone(self.fixture.journal.read(self.child))
        self.assertFalse(self.fixture.worker.run_cycle())
        self.assertEqual(self.state.get_provider_job(self.tail).status, "queued")

    def test_competing_recovery_claims_only_parent_and_settles_one_saved_result(self) -> None:
        text = self.save_completion()
        self.expire()
        claimed = self.fixture.journal.claim_stale("claude", "example-first-recovery")
        assert claimed is not None and claimed.lease_token
        self.assertEqual(claimed.job_id, self.parent)
        competitor = HubState.open_existing(self.fixture.fixture.path)
        self.addCleanup(competitor.close)
        self.assertIsNone(
            ExecutionJournal(competitor).claim_stale("claude", "example-second-recovery")
        )
        native = self.fixture.fixture
        self.assertTrue(
            recover_claude_completion(
                self.state,
                native.fixture.config,
                native.fixture.registry,
                "claude",
                self.parent,
                claimed.lease_token,
            )
        )
        self.assertFalse(
            recover_claude_completion(
                self.state,
                native.fixture.config,
                native.fixture.registry,
                "claude",
                self.parent,
                claimed.lease_token,
            )
        )
        self.assertEqual(self.state.get_provider_job(self.parent).status, "result_ready")
        self.assertEqual(self.state.get_provider_job(self.child).status, "completed")
        self.assertEqual(self.state.get_provider_result(self.parent).visible_response, text)
        for table in ("provider_job_results", "telegram_outbox", "external_turn_excerpts"):
            self.assertEqual(
                self.state._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1
            )
        self.assertIsNone(self.fixture.journal.read(self.child))
        self.assertFalse(self.fixture.worker.run_cycle())
        self.assertEqual(self.state.get_provider_job(self.tail).status, "queued")

    def test_partial_delivery_receipt_and_unknown_next_part_survive_reopen_without_resend(
        self,
    ) -> None:
        self.save_completion()
        self.expire()
        self.assertTrue(self.fixture.worker.run_cycle())
        first = self.state.lease_telegram_outbox("claude", "example-sender")
        assert first and first.lease_token
        part = self.state.delivery.next_outbox_part(first.outbox_id, first.lease_token)
        self.state.delivery.begin_outbox_send(first.outbox_id, first.lease_token, part.part_index)
        self.state.delivery.mark_outbox_delivered(
            first.outbox_id,
            first.lease_token,
            part_index=part.part_index,
            telegram_message_id=201,
        )
        second = self.state.lease_telegram_outbox("claude", "example-sender")
        assert second and second.lease_token
        next_part = self.state.delivery.next_outbox_part(second.outbox_id, second.lease_token)
        self.state.delivery.begin_outbox_send(
            second.outbox_id, second.lease_token, next_part.part_index
        )
        self.assertTrue(
            self.state.delivery.mark_outbox_unknown(
                second.outbox_id,
                second.lease_token,
                next_part.part_index,
                error_code="example_unknown",
            )
        )
        tables = (
            "provider_jobs",
            "claude_input_runs",
            "claude_input_children",
            "provider_execution_checkpoints",
            "provider_job_results",
            "external_turn_excerpts",
            "telegram_outbox",
            "telegram_outbox_parts",
            "telegram_send_attempts",
            "telegram_send_receipts",
        )
        before = {
            table: self.state._connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }
        reopened = HubState.open_existing(self.fixture.fixture.path)
        self.addCleanup(reopened.close)
        reopened.recover_stale_telegram_outbox()
        self.assertIsNone(reopened.lease_telegram_outbox("claude", "example-new-sender"))
        for table in tables:
            self.assertEqual(
                reopened._connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(),
                before[table],
            )
        parts = reopened.get_telegram_outbox_parts(first.outbox_id)
        self.assertEqual(
            (parts[0].telegram_message_id, parts[0].receipt_validation_version), (201, 1)
        )
        self.assertIsNone(parts[1].telegram_message_id)
        self.assertFalse(self.fixture.worker.run_cycle())
        self.assertEqual(self.state.get_provider_job(self.tail).status, "queued")

    def test_covering_stop_settles_child_without_replay_and_keeps_tail_cancelled(self) -> None:
        self.save_completion()
        parent = self.state.get_provider_job(self.parent)
        self.state.request_emergency_stop(
            topic_id=parent.topic_id,
            chat_id=parent.chat_id,
            message_id=99,
            target_agent_id="claude",
        )
        self.expire()
        self.assertTrue(self.fixture.worker.run_cycle())
        for job_id in (self.parent, self.child, self.tail):
            self.assertEqual(self.state.get_provider_job(job_id).status, "cancelled")
        self.assertIsNone(
            self.state._connection.execute("SELECT 1 FROM provider_job_results").fetchone()
        )
        self.assertFalse(self.fixture.worker.run_cycle())


if __name__ == "__main__":
    unittest.main()
