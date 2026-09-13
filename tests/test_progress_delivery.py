from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.progress_delivery import ProgressDeliveryQueue
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError


class RecordingBot:
    def __init__(self, *, retry_after: int | None = None) -> None:
        self.retry_after = retry_after
        self.sent: list[tuple[int, int, str]] = []

    def send_html(self, chat_id: int, thread_id: int, html: str) -> int:
        self.sent.append((chat_id, thread_id, html))
        if self.retry_after is not None:
            retry_after = self.retry_after
            self.retry_after = None
            raise TelegramError(
                "rate limited",
                operation="send_message",
                failure_class="rate_limit",
                status_code=429,
                retry_after=retry_after,
            )
        return len(self.sent)

    def send_chat_action(self, chat_id: int, thread_id: int, action: str = "typing") -> None:
        pass

    def send_message_draft(
        self, chat_id: int, thread_id: int, *, draft_id: int, text: str = ""
    ) -> None:
        pass


class DurableProgressDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.root = base / "project"
        self.root.mkdir()
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example-project", -1001234567890),),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-5.6-sol",
                    "high",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        self.state = HubState.open(self.config.state_path)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def executing_job(self, *, message_id: int = 1) -> tuple[str, str, ExecutionJournal]:
        topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "gpt-5.6-sol", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"telegram:-1001234567890:{message_id}",
            chat_id=-1001234567890,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=None,
            model=session.model,
            effort=session.effort,
            payload_text="durable task",
            context_watermark=None,
            handoff_id=None,
        )
        leased = self.state.lease_provider_job("codex", "test-worker")
        assert leased is not None and leased.job_id == job.job_id and leased.lease_token
        executing = self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        assert executing.lease_token
        journal = ExecutionJournal(self.state, progress_enabled=True)
        journal.record_thread(job.job_id, executing.lease_token, "thread-example", self.root)
        journal.record_turn(job.job_id, executing.lease_token, "turn-example")
        return job.job_id, executing.lease_token, journal

    def sender(self, bot: RecordingBot) -> TelegramOutboxSender:
        return TelegramOutboxSender(
            self.config,
            telegram_bots=cast(dict[str, Any], {"codex": bot}),
            sender_id="test-progress-sender",
        )

    def test_journal_queues_only_rate_limited_commentary_and_deduplicates(self) -> None:
        job_id, token, journal = self.executing_job()
        journal.record_item(job_id, token, "item-1", "Working <safely>", "commentary")
        journal.record_item(job_id, token, "item-1", "Working <safely>", "commentary")
        journal.record_item(job_id, token, "item-2", "Too soon", "commentary")
        journal.record_item(job_id, token, "item-3", "Final", "final_answer")

        queued = ProgressDeliveryQueue(self.state).for_job(job_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].item_sequence, 1)
        self.assertIn("Working &lt;safely&gt;", queued[0].telegram_html)

        old = (datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat()
        self.state._connection.execute(
            "UPDATE provider_progress_deliveries SET created_at = ? WHERE progress_id = ?",
            (old, queued[0].progress_id),
        )
        self.state._connection.commit()
        journal.record_item(job_id, token, "item-4", "Later progress", "commentary")
        self.assertEqual(len(ProgressDeliveryQueue(self.state).for_job(job_id)), 2)

    def test_sender_uses_provider_identity_without_completing_job(self) -> None:
        job_id, token, journal = self.executing_job()
        journal.record_item(job_id, token, "item-1", "Still working", "commentary")
        bot = RecordingBot()
        sender = self.sender(bot)
        try:
            self.assertTrue(sender.run_cycle())
        finally:
            sender.close()

        progress = ProgressDeliveryQueue(self.state).for_job(job_id)[0]
        self.assertEqual(progress.status, "delivered")
        self.assertEqual(bot.sent, [(-1001234567890, 77, progress.telegram_html)])
        job = self.state.get_provider_job(job_id)
        self.assertEqual(job.status, "executing")
        self.assertEqual(job.attempt_count, 1)

    def test_terminal_job_supersedes_pending_progress(self) -> None:
        job_id, token, journal = self.executing_job()
        journal.record_item(job_id, token, "item-1", "Will become stale", "commentary")
        self.state.fail_provider_job(
            job_id,
            token,
            error_class="provider_failure",
            error_code="example_failure",
        )
        bot = RecordingBot()
        sender = self.sender(bot)
        try:
            self.assertFalse(sender.run_cycle())
        finally:
            sender.close()
        self.assertEqual(ProgressDeliveryQueue(self.state).for_job(job_id)[0].status, "superseded")
        self.assertEqual(bot.sent, [])

    def test_final_result_has_priority_and_supersedes_pending_progress(self) -> None:
        job_id, token, journal = self.executing_job()
        journal.record_item(job_id, token, "item-1", "Nearly done", "commentary")
        self.state.commit_provider_result(
            job_id,
            token,
            visible_response="Done",
            sender_agent_id="codex",
            telegram_html="<b>Done</b>",
        )
        bot = RecordingBot()
        sender = self.sender(bot)
        try:
            self.assertTrue(sender.run_cycle())
            self.assertFalse(sender.run_cycle())
        finally:
            sender.close()
        self.assertEqual(bot.sent, [(-1001234567890, 77, "<b>Done</b>")])
        self.assertEqual(ProgressDeliveryQueue(self.state).for_job(job_id)[0].status, "superseded")
        self.assertEqual(self.state.get_provider_job(job_id).status, "completed")

    def test_retry_after_survives_sender_restart(self) -> None:
        job_id, _token, journal = self.executing_job()
        journal.record_item(job_id, _token, "item-1", "Retry me", "commentary")
        clock = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self.state._connection.execute(
            "UPDATE provider_progress_deliveries SET available_at = ? WHERE job_id = ?",
            (clock.isoformat(), job_id),
        )
        self.state._connection.commit()

        bot = RecordingBot(retry_after=60)
        sender = self.sender(bot)
        try:
            self.assertTrue(sender.run_cycle(now=clock))
        finally:
            sender.close()
        pending = ProgressDeliveryQueue(self.state).for_job(job_id)[0]
        self.assertEqual(pending.status, "pending")
        self.assertEqual(
            datetime.fromisoformat(pending.available_at), clock + timedelta(seconds=60)
        )

        restarted = self.sender(bot)
        try:
            self.assertFalse(restarted.run_cycle(now=clock + timedelta(seconds=59)))
            self.assertTrue(restarted.run_cycle(now=clock + timedelta(seconds=60)))
        finally:
            restarted.close()
        self.assertEqual(ProgressDeliveryQueue(self.state).for_job(job_id)[0].status, "delivered")
        self.assertEqual(len(bot.sent), 2)


if __name__ == "__main__":
    unittest.main()
