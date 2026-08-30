from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.state import HubState, StateError


class ProviderJobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "hub.db"
        self.state = HubState.open(self.path)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example topic",
        )
        self.codex = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def enqueue(
        self,
        message_id: int,
        *,
        agent_id: str = "codex",
        session_id: str | None = None,
        generation: int | None = None,
    ):
        return self.state.enqueue_provider_job(
            idempotency_key=f"telegram:-1001234567890:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id=agent_id,
            session_id=session_id or self.codex.session_id,
            session_generation=generation or self.codex.generation,
            provider_session_id="provider-session-example",
            model="gpt-example",
            effort="high",
            payload_text=f"bounded request {message_id}",
            context_watermark=12,
        )

    def test_open_enables_wal_foreign_keys_and_busy_timeout(self) -> None:
        connection = self.state._connection
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_enqueue_is_idempotent_and_snapshots_bounded_fields(self) -> None:
        first, created = self.enqueue(501)
        duplicate, duplicate_created = self.enqueue(501)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.job_id, duplicate.job_id)
        self.assertEqual(first.topic_sequence, 1)
        self.assertEqual(first.status, "queued")
        self.assertEqual(first.session_generation, self.codex.generation)
        self.assertEqual(first.context_watermark, 12)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM observed_messages WHERE chat_id = ? AND message_id = ?",
                (self.topic.chat_id, 501),
            ).fetchone()[0],
            1,
        )

    def test_enqueue_rejects_oversized_or_mismatched_snapshot(self) -> None:
        with self.assertRaisesRegex(StateError, "payload"):
            self.state.enqueue_provider_job(
                idempotency_key="telegram:oversized",
                chat_id=self.topic.chat_id,
                message_id=502,
                topic_id=self.topic.topic_id,
                agent_id="codex",
                session_id=self.codex.session_id,
                session_generation=self.codex.generation,
                model="gpt-example",
                effort="high",
                payload_text="x" * 20001,
            )
        with self.assertRaisesRegex(StateError, "session snapshot"):
            self.enqueue(503, generation=self.codex.generation + 1)

    def test_strict_fifo_blocks_other_provider_in_same_topic(self) -> None:
        first, _ = self.enqueue(510)
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "opencode", "provider-selected", "high"
        )
        second, _ = self.enqueue(
            511,
            agent_id="opencode",
            session_id=satellite.session_id,
            generation=satellite.generation,
        )

        self.assertIsNone(self.state.lease_provider_job("opencode", "worker-open"))
        leased = self.state.lease_provider_job("codex", "worker-codex")
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, first.job_id)
        self.assertTrue(leased.lease_token)
        self.state.fail_provider_job(
            first.job_id, leased.lease_token or "", error_class="permanent", error_code="bad"
        )
        next_job = self.state.lease_provider_job("opencode", "worker-open")
        self.assertIsNotNone(next_job)
        assert next_job is not None
        self.assertEqual(next_job.job_id, second.job_id)

    def test_lease_token_guards_execution_heartbeat_and_result(self) -> None:
        queued, _ = self.enqueue(520)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.mark_provider_job_executing(queued.job_id, "wrong-token")

        executing = self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)
        self.assertEqual(executing.status, "executing")
        self.assertEqual(executing.attempt_count, 1)
        renewed = self.state.heartbeat_provider_job(
            queued.job_id, leased.lease_token, lease_seconds=180
        )
        self.assertGreater(renewed.lease_expires_at or "", leased.lease_expires_at or "")
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.commit_provider_result(
                queued.job_id,
                "wrong-token",
                visible_response="done",
                sender_agent_id="codex",
                telegram_html="done",
            )

    def test_stale_leased_requeues_but_stale_executing_is_indeterminate(self) -> None:
        first, _ = self.enqueue(530)
        second_topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=78,
            title="Second topic",
        )
        second_session = self.state.activate_agent(
            second_topic.topic_id, "codex", "gpt-example", "high"
        )
        second, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:531",
            chat_id=second_topic.chat_id,
            message_id=531,
            topic_id=second_topic.topic_id,
            agent_id="codex",
            session_id=second_session.session_id,
            session_generation=second_session.generation,
            model="gpt-example",
            effort="high",
            payload_text="second bounded request",
        )
        past = datetime(2026, 1, 1, tzinfo=timezone.utc)
        leased_first = self.state.lease_provider_job(
            "codex", "worker-one", lease_seconds=1, now=past
        )
        leased_second = self.state.lease_provider_job(
            "codex", "worker-two", lease_seconds=1, now=past
        )
        assert leased_first is not None and leased_first.lease_token is not None
        assert leased_second is not None and leased_second.lease_token is not None
        self.state.mark_provider_job_executing(second.job_id, leased_second.lease_token, now=past)

        recovered = self.state.recover_stale_provider_jobs(now=past + timedelta(seconds=2))

        self.assertEqual(recovered.requeued_job_ids, (first.job_id,))
        self.assertEqual(recovered.indeterminate_job_ids, (second.job_id,))
        self.assertEqual(self.state.get_provider_job(first.job_id).status, "queued")
        self.assertEqual(self.state.get_provider_job(second.job_id).status, "indeterminate")

    def test_result_and_outbox_commit_atomically_then_delivery_completes_job(self) -> None:
        queued, _ = self.enqueue(540)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)

        result = self.state.commit_provider_result(
            queued.job_id,
            leased.lease_token,
            visible_response="bounded visible result",
            sender_agent_id="codex",
            telegram_html="<b>bounded result</b>",
            provider_session_id="provider-session-after",
            actual_model="gpt-example",
            safe_metadata_json='{"quota":"ok"}',
            acknowledge_context=True,
        )

        self.assertEqual(result.job_id, queued.job_id)
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "result_ready")
        outbox = self.state.get_telegram_outbox_for_job(queued.job_id)
        self.assertEqual(outbox.status, "pending")
        sending = self.state.lease_telegram_outbox("codex", "sender-one")
        assert sending is not None and sending.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            sending.outbox_id, sending.lease_token, telegram_message_id=9001
        )
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "completed")
        self.assertEqual(
            self.state.get_telegram_outbox_for_job(queued.job_id).telegram_message_id, 9001
        )

    def test_retry_is_allowed_only_before_execution_and_is_bounded(self) -> None:
        queued, _ = self.enqueue(550)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        retry = self.state.schedule_provider_job_retry(
            queued.job_id,
            leased.lease_token,
            error_code="network-before-start",
            delay_seconds=15,
        )
        self.assertEqual(retry.status, "retry_wait")
        with self.assertRaisesRegex(StateError, "pre-execution"):
            self.state.schedule_provider_job_retry(
                queued.job_id,
                leased.lease_token,
                error_code="again",
                delay_seconds=15,
            )

    def test_concurrent_enqueue_assigns_unique_contiguous_topic_sequence(self) -> None:
        session_id = self.codex.session_id
        generation = self.codex.generation
        self.state.close()
        barrier = threading.Barrier(6)
        sequences: list[int] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def enqueue(index: int) -> None:
            state = HubState.open(self.path)
            try:
                barrier.wait()
                job, _ = state.enqueue_provider_job(
                    idempotency_key=f"telegram:concurrent:{index}",
                    chat_id=self.topic.chat_id,
                    message_id=600 + index,
                    topic_id=self.topic.topic_id,
                    agent_id="codex",
                    session_id=session_id,
                    session_generation=generation,
                    model="gpt-example",
                    effort="high",
                    payload_text=f"request {index}",
                )
                with lock:
                    sequences.append(job.topic_sequence)
            except BaseException as exc:  # pragma: no cover - diagnostic collection
                with lock:
                    failures.append(exc)
            finally:
                state.close()

        threads = [threading.Thread(target=enqueue, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.state = HubState.open(self.path)

        self.assertEqual(failures, [])
        self.assertEqual(sorted(sequences), [1, 2, 3, 4, 5, 6])

    def test_concurrent_workers_cannot_lease_the_same_job(self) -> None:
        queued, _ = self.enqueue(559)
        self.state.close()
        barrier = threading.Barrier(4)
        claimed: list[str] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def lease(index: int) -> None:
            state = HubState.open(self.path)
            try:
                barrier.wait()
                job = state.lease_provider_job("codex", f"worker-{index}")
                if job is not None:
                    with lock:
                        claimed.append(job.job_id)
            except BaseException as exc:  # pragma: no cover - diagnostic collection
                with lock:
                    failures.append(exc)
            finally:
                state.close()

        threads = [threading.Thread(target=lease, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.state = HubState.open(self.path)

        self.assertEqual(failures, [])
        self.assertEqual(claimed, [queued.job_id])

    def test_queue_state_survives_close_and_reopen(self) -> None:
        queued, _ = self.enqueue(560)
        self.state.close()
        self.state = HubState.open(self.path)
        reopened = self.state.get_provider_job(queued.job_id)
        self.assertEqual((reopened.status, reopened.topic_sequence), ("queued", 1))


if __name__ == "__main__":
    unittest.main()
