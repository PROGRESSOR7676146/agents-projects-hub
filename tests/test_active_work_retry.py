from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_activity import CodexActivityEvent
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.task_activity import TaskActivityState
from hermes_codex_router.work_retry_state import WorkRetryState
from tests.hub_service_harness import CHAT_ID, CODEX, THREAD_ID, HubHarness, text_update


class ActiveWorkRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.harness = HubHarness(Path(self.temp.name))
        self.addCleanup(self.harness.close)
        self.harness.with_config(
            hub_bot=HubTelegramBot("example_hub_bot", Path(self.temp.name) / "unused-token"),
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        self.harness.service.ingress_identity = "hub"
        self.session = self.harness.activate(CODEX)
        self.topic = self.harness.topic()
        self.state = self.harness.service.state
        self.now = datetime.now(timezone.utc)
        self.job, _ = self.state.enqueue_provider_job(
            idempotency_key="example:task",
            chat_id=CHAT_ID,
            message_id=20,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=self.session.provider_session_id,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Example original task",
        )
        with self.state._immediate_transaction():
            notice, _ = self.state.task_notices.prepare_notice_in_transaction(
                event_key="example:accepted",
                kind="accepted",
                job_id=self.job.job_id,
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                telegram_html="Example accepted task",
                now=self.now,
            )
        delivery = self.state.task_notices.lease_notice("example-sender", now=self.now)
        assert delivery is not None and delivery.lease_token is not None
        self.state.task_notices.begin_send(notice.notice_id, delivery.lease_token, now=self.now)
        self.state.task_notices.complete_send(
            notice.notice_id, delivery.lease_token, telegram_message_id=101, now=self.now
        )

    def start_job(self):
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        return self.state.mark_provider_job_executing(self.job.job_id, lease.lease_token)

    def execution_snapshot(self):
        return {
            table: [
                {
                    key: row[key]
                    for key in row.keys()
                    if not (table == "topics" and key == "updated_at")
                }
                for row in self.state._connection.execute(
                    f"SELECT * FROM {table} ORDER BY 1"
                ).fetchall()
            ]
            for table in (
                "provider_jobs",
                "provider_job_inputs",
                "agent_sessions",
                "topics",
                "provider_execution_checkpoints",
                "provider_job_holds",
                "provider_stop_requests",
                "task_activity",
                "task_activity_entries",
                "provider_turn_terminal_evidence",
                "provider_job_resolutions",
            )
        }

    def retry(self, message_id: int = 30, source: int = 101):
        update = text_update(message_id, "retry")
        cast(dict[str, Any], update["message"])["reply_to_message"] = {"message_id": source}
        return self.harness.service.handle_update(update)

    def report(self, *, message_id: int = 30, chat_id: int = CHAT_ID, thread_id: int = THREAD_ID):
        return WorkRetryState(self.state).report_from_notice(
            chat_id=chat_id,
            thread_id=thread_id,
            notice_message_id=101,
            reply_message_id=message_id,
            canonical_root=self.harness.root,
            now=self.now,
        )

    def retry_notices(self):
        return self.state._connection.execute(
            "SELECT * FROM task_lifecycle_notices WHERE kind='retry_report' ORDER BY created_at"
        ).fetchall()

    def test_active_retry_reports_same_work_without_any_execution_mutation(self) -> None:
        self.start_job()
        before = self.execution_snapshot()
        with patch.object(
            self.harness.client, "start_turn", side_effect=AssertionError("no inference")
        ):
            self.assertTrue(self.retry())
        self.assertEqual(self.execution_snapshot(), before)
        self.assertEqual(len(self.retry_notices()), 1)
        notice = self.retry_notices()[0]
        self.assertEqual((notice["job_id"], notice["reply_to_message_id"]), (self.job.job_id, 30))
        self.assertIn("same request", notice["telegram_html"])
        self.assertIn("No new run", notice["telegram_html"])
        self.assertEqual(self.harness.client.turns, 0)

    def test_duplicate_retry_after_restart_keeps_one_report_and_original_job(self) -> None:
        self.start_job()
        self.assertIsNotNone(self.report())
        before = self.execution_snapshot()
        reopened = HubState.open(self.harness.config.state_path)
        try:
            repeated = WorkRetryState(reopened).report_from_notice(
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                notice_message_id=101,
                reply_message_id=30,
                canonical_root=self.harness.root,
                now=self.now,
            )
            assert repeated is not None
            self.assertFalse(repeated.created)
        finally:
            reopened.close()
        self.assertEqual(len(self.retry_notices()), 1)
        self.assertEqual(self.execution_snapshot(), before)

    def test_topic_and_chat_mismatches_never_select_work_or_claim_input(self) -> None:
        for chat_id, thread_id in ((CHAT_ID, THREAD_ID + 1), (CHAT_ID - 1, THREAD_ID)):
            with self.subTest(chat_id=chat_id, thread_id=thread_id):
                self.assertIsNone(self.report(chat_id=chat_id, thread_id=thread_id))
                self.assertFalse(self.state.message_already_observed(chat_id, 30))
        self.assertEqual(self.retry_notices(), [])

    def test_unknown_outcome_report_does_not_release_root_or_repeat_work(self) -> None:
        executing = self.start_job()
        assert executing.lease_token is not None
        other = self.state.observe_topic(
            project_id="example-project",
            chat_id=CHAT_ID,
            thread_id=THREAD_ID + 1,
            title="Example other topic",
            execution_root=self.harness.root,
        )
        session = self.state.activate_agent(other.topic_id, "codex", "example-model", "high")
        self.state.enqueue_provider_job(
            idempotency_key="example:other-task",
            chat_id=CHAT_ID,
            message_id=21,
            topic_id=other.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Example later same-root work",
        )
        self.state.mark_provider_job_indeterminate(
            self.job.job_id, executing.lease_token, error_code="example_unknown"
        )
        before = self.execution_snapshot()
        self.assertIsNotNone(self.report())
        self.assertEqual(self.execution_snapshot(), before)
        self.assertIn("unconfirmed", self.retry_notices()[0]["telegram_html"])
        self.assertIsNone(self.state.lease_provider_job("codex", "example-other-worker"))

    def test_duplicate_message_in_wrong_topic_cannot_return_original_notice(self) -> None:
        self.assertIsNotNone(self.report())
        mismatched = self.report(thread_id=THREAD_ID + 1)
        assert mismatched is not None
        self.assertFalse(mismatched.created)
        self.assertIsNone(mismatched.notice)
        self.assertEqual(len(self.retry_notices()), 1)

    def test_receipt_and_observed_input_roll_back_together_on_fault(self) -> None:
        with patch.object(
            self.state.task_notices,
            "prepare_notice_in_transaction",
            side_effect=RuntimeError("example fault"),
        ):
            with self.assertRaisesRegex(RuntimeError, "example fault"):
                self.report()
        self.assertFalse(self.state.message_already_observed(CHAT_ID, 30))
        self.assertEqual(self.retry_notices(), [])

    def test_unknown_reply_fails_visibly_without_enqueuing_retry(self) -> None:
        before = self.execution_snapshot()
        self.assertTrue(self.retry(source=999))
        self.assertIn("retry", self.harness.last_reply.lower())
        self.assertEqual(self.execution_snapshot(), before)

    def test_unsupported_material_with_retry_text_keeps_its_unavailability_receipt(self) -> None:
        for message_id, text_source in ((31, "caption"), (32, "text")):
            with self.subTest(text_source=text_source):
                update = text_update(message_id, "retry")
                message = cast(dict[str, Any], update["message"])
                if text_source == "caption":
                    message["caption"] = message.pop("text")
                message["video"] = {"file_id": "example-video"}
                message["reply_to_message"] = {"message_id": 101}
                self.assertTrue(self.harness.service.handle_update(update))
                self.assertEqual(self.retry_notices(), [])
                materials = [
                    material
                    for job in self.state.provider_jobs_for_topic(self.topic.topic_id)
                    for material in self.state.incoming_materials_for_job(job.job_id)
                    if material.message_id == message_id
                ]
                self.assertEqual(len(materials), 1)
                self.assertEqual(materials[0].status, "unavailable")
                self.assertEqual(materials[0].unavailable_detail, "video input is not supported")
                self.assertEqual(self.harness.client.turns, 0)

    def test_caption_only_reply_retains_ordinary_input_routing(self) -> None:
        update = text_update(33, "retry")
        message = cast(dict[str, Any], update["message"])
        message["caption"] = message.pop("text")
        message["reply_to_message"] = {"message_id": 101}
        self.assertTrue(self.harness.service.handle_update(update))
        self.assertEqual(self.retry_notices(), [])
        self.assertEqual(len(self.state.provider_jobs_for_topic(self.topic.topic_id)), 2)
        self.assertEqual(self.harness.client.turns, 0)

    def test_retry_reports_pending_owner_hold_without_confirming_or_releasing_it(self) -> None:
        # The accepted notice was delivered before the job entered a durable hold.
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at,hold_reason) "
                "VALUES(?,?,?,'local')",
                (self.job.job_id, self.job.job_id, self.now.isoformat()),
            )
        before = self.execution_snapshot()
        self.assertTrue(self.retry())
        text = self.retry_notices()[0]["telegram_html"]
        self.assertIn("required your decision", text)
        self.assertNotIn("Wait for its existing result", text)
        self.assertEqual(self.execution_snapshot(), before)
        self.assertIsNone(self.state.lease_provider_job("codex", "example-other-worker"))

    def test_unsupported_embedded_mode_rejects_control_without_inference(self) -> None:
        self.harness.with_config(queue_runtime="embedded")
        before = self.execution_snapshot()
        self.assertTrue(self.retry())
        self.assertIn("unsupported", self.harness.last_reply.lower())
        self.assertEqual(self.execution_snapshot(), before)

    def test_changed_writer_reports_changed_binding_without_taking_it_back(self) -> None:
        self.start_job()
        # Simulate a binding changed outside the normal guarded transfer API.
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (self.session.session_id,),
            )
        before = self.execution_snapshot()
        self.assertIsNotNone(self.report())
        self.assertIn("binding had changed", self.retry_notices()[0]["telegram_html"])
        self.assertEqual(self.execution_snapshot(), before)

    def test_expired_execution_lease_is_reported_as_unconfirmed(self) -> None:
        self.start_job()
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
                ((self.now - timedelta(seconds=1)).isoformat(), self.job.job_id),
            )
        before = self.execution_snapshot()
        self.assertIsNotNone(self.report())
        self.assertIn("unconfirmed", self.retry_notices()[0]["telegram_html"])
        self.assertEqual(self.execution_snapshot(), before)

    def test_existing_human_approval_is_reported_without_resolving_it(self) -> None:
        executing = self.start_job()
        assert executing.lease_token is not None
        token = executing.lease_token
        journal = ExecutionJournal(self.state)
        journal.record_thread(self.job.job_id, token, "provider-session-1", self.harness.root)
        journal.record_turn(self.job.job_id, token, "example-turn")
        activity = TaskActivityState(
            self.state._connection,
            transaction=self.state._immediate_transaction,
            state_error=StateError,
            notices=self.state.task_notices,
        )
        activity.bind_accepted(
            self.job.job_id,
            token,
            "provider-session-1",
            "example-turn",
            str(self.harness.root),
            now=self.now,
        )
        activity.record_activity(
            self.job.job_id,
            token,
            CodexActivityEvent(
                "approval_requested",
                "command",
                "provider-session-1",
                "example-turn",
                "example-tool",
                request_id=5,
            ),
            now=self.now,
        )
        before = self.execution_snapshot()
        self.assertIsNotNone(self.report())
        self.assertEqual(self.execution_snapshot(), before)
        text = self.retry_notices()[0]["telegram_html"]
        self.assertIn("human approval", text)
        self.assertIn("Codex/tlive", text)
        self.assertIn("does not approve", text)
        self.assertNotIn("example-tool", text)
        self.assertNotIn(str(self.harness.root), text)

    def test_existing_owner_resolution_is_not_reported_as_unknown_exclusion(self) -> None:
        executing = self.start_job()
        assert executing.lease_token is not None
        self.state.mark_provider_job_indeterminate(
            self.job.job_id, executing.lease_token, error_code="example_unknown"
        )
        self.state.resolve_indeterminate_job(self.job.job_id, "acknowledged")
        before = self.execution_snapshot()
        self.assertIsNotNone(self.report())
        text = self.retry_notices()[0]["telegram_html"]
        self.assertIn("owner resolution", text)
        self.assertNotIn("unconfirmed", text)
        self.assertEqual(self.execution_snapshot(), before)

    def test_later_state_change_does_not_rewrite_or_resend_the_retry_snapshot(self) -> None:
        executing = self.start_job()
        assert executing.lease_token is not None
        first = self.report()
        assert first is not None and first.notice is not None
        self.state.mark_provider_job_indeterminate(
            self.job.job_id, executing.lease_token, error_code="example_unknown"
        )
        repeated = self.report()
        assert repeated is not None and repeated.notice is not None
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.notice.telegram_html, first.notice.telegram_html)
        self.assertEqual(len(self.retry_notices()), 1)
        delivery = self.state.task_notices.lease_notice("example-sender", now=self.now)
        assert delivery is not None and delivery.lease_token is not None
        begun = self.state.task_notices.begin_send(
            delivery.notice_id, delivery.lease_token, now=self.now
        )
        self.assertEqual(begun.status, "leased")
        self.state.task_notices.mark_send_unknown(
            delivery.notice_id,
            delivery.lease_token,
            error_code="example_lost_response",
            now=self.now,
        )
        self.assertIsNone(self.state.task_notices.lease_notice("example-sender", now=self.now))
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "indeterminate")


if __name__ == "__main__":
    unittest.main()
