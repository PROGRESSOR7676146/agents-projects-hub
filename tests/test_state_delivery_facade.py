from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.progress_delivery import (
    ProgressDeliveryQueue,
    ProgressDeliveryRecord,
)
from hermes_codex_router.state import (
    HubState,
    ProviderJobRecord,
    TelegramOutboxPartRecord,
    TelegramOutboxRecord,
)
from hermes_codex_router.state_delivery import (
    ProgressDeliveryRecord as FacadeProgressDeliveryRecord,
)
from hermes_codex_router.state_delivery import (
    TelegramOutboxPartRecord as FacadeTelegramOutboxPartRecord,
)
from hermes_codex_router.state_delivery import (
    TelegramOutboxRecord as FacadeTelegramOutboxRecord,
)


class DeliveryStateFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.state = HubState.open(self.base / "private" / "hub.db")
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Fictional delivery topic",
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "codex", "fictional-model", "high"
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def executing_job(self, message_id: int) -> tuple[ProviderJobRecord, str]:
        job, created = self.state.enqueue_provider_job(
            idempotency_key=f"delivery:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id=self.session.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text=f"fictional delivery request {message_id}",
        )
        self.assertTrue(created)
        leased = self.state.lease_provider_job("codex", "fictional-worker")
        assert leased is not None and leased.lease_token is not None
        executing = self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        token = executing.lease_token
        assert token is not None
        return executing, token

    def ready_outbox(self, message_id: int) -> TelegramOutboxRecord:
        executing, token = self.executing_job(message_id)
        self.state.commit_provider_result(
            executing.job_id,
            token,
            visible_response="fictional result",
            sender_agent_id="codex",
            telegram_html="fictional result",
        )
        return self.state.get_telegram_outbox_for_job(executing.job_id)

    def test_facade_uses_hub_connection_and_preserves_public_record_types(self) -> None:
        self.assertIs(self.state._delivery_state._connection, self.state._connection)
        self.assertEqual(
            self.state._delivery_state._transaction,
            self.state._immediate_transaction,
        )
        self.assertEqual(
            self.state._delivery_state._write_transaction,
            self.state._connection_transaction,
        )
        self.assertIs(TelegramOutboxRecord, FacadeTelegramOutboxRecord)
        self.assertIs(TelegramOutboxPartRecord, FacadeTelegramOutboxPartRecord)
        self.assertIs(ProgressDeliveryRecord, FacadeProgressDeliveryRecord)

        queue = ProgressDeliveryQueue(self.state)
        self.assertIs(queue.connection, self.state._connection)
        self.assertIs(queue._delivery_state, self.state._delivery_state)

    def test_outbox_lease_fault_rolls_back_through_hub_transaction_owner(self) -> None:
        outbox = self.ready_outbox(801)
        transaction = self.state._delivery_state._transaction

        @contextmanager
        def fail_after_lease() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-outbox-lease fault")

        with patch.object(self.state._delivery_state, "_transaction", fail_after_lease):
            with self.assertRaisesRegex(RuntimeError, "post-outbox-lease fault"):
                self.state.lease_telegram_outbox("codex", "fictional-sender")

        restored = self.state.get_telegram_outbox(outbox.outbox_id)
        self.assertEqual((restored.status, restored.attempt_count), ("pending", 0))
        self.assertIsNone(restored.lease_token)

    def test_outbox_write_fault_rolls_back_through_hub_transaction_owner(self) -> None:
        outbox = self.ready_outbox(802)
        leased = self.state.lease_telegram_outbox("codex", "fictional-sender")
        assert leased is not None and leased.lease_token is not None
        transaction = self.state._delivery_state._write_transaction

        @contextmanager
        def fail_after_write() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-outbox-write fault")

        with patch.object(self.state._delivery_state, "_write_transaction", fail_after_write):
            with self.assertRaisesRegex(RuntimeError, "post-outbox-write fault"):
                self.state.release_telegram_outbox_lease(leased.outbox_id, leased.lease_token)

        restored = self.state.get_telegram_outbox(outbox.outbox_id)
        self.assertEqual((restored.status, restored.attempt_count), ("sending", 1))
        self.assertEqual(restored.lease_token, leased.lease_token)

    def test_progress_fault_rolls_back_journal_item_and_delivery_together(self) -> None:
        executing, token = self.executing_job(803)
        journal = ExecutionJournal(self.state, progress_enabled=True)
        project_root = self.base / "fictional-project"
        project_root.mkdir()
        journal.record_thread(
            executing.job_id,
            token,
            "fictional-thread",
            project_root,
        )
        journal.record_turn(
            executing.job_id,
            token,
            "fictional-turn",
        )
        original = self.state._delivery_state.enqueue_progress_in_transaction

        def fail_after_progress(*args: object, **kwargs: object) -> bool:
            original(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("fictional post-progress fault")

        with patch.object(
            self.state._delivery_state,
            "enqueue_progress_in_transaction",
            fail_after_progress,
        ):
            with self.assertRaisesRegex(RuntimeError, "post-progress fault"):
                journal.record_item(
                    executing.job_id,
                    token,
                    "fictional-item",
                    "Fictional progress",
                    "commentary",
                )

        visible_count = self.state._connection.execute(
            "SELECT COUNT(*) FROM provider_visible_items WHERE job_id = ?",
            (executing.job_id,),
        ).fetchone()
        assert visible_count is not None
        self.assertEqual(int(visible_count[0]), 0)
        self.assertEqual(ProgressDeliveryQueue(self.state).for_job(executing.job_id), ())
        self.assertFalse(self.state._connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
