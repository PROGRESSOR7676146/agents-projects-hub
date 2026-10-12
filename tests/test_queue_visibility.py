from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from hermes_codex_router.provider_queue_capacity import QueueCapacityConfig
from hermes_codex_router.state import HubState
from hermes_codex_router.task_notice_sender import deliver_task_notice


class QueueVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.topic, self.session = self.topic_session("example-project", 7)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def topic_session(self, project_id: str, thread_id: int):
        topic = self.state.observe_topic(
            project_id=project_id,
            chat_id=-1001234567890,
            thread_id=thread_id,
            title="Fictional topic",
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "fictional-model", "high")
        return topic, session

    def enqueue(
        self,
        message_id: int,
        *,
        enabled: bool = True,
        topic=None,
        session=None,
        batch: bool = False,
        capacity: QueueCapacityConfig | None = None,
    ):
        topic = topic or self.topic
        session = session or self.session
        arguments: dict[str, Any] = dict(
            idempotency_key=f"fictional:{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Fictional request",
            prepare_task_notices=enabled,
            queue_capacity=capacity,
        )
        if batch:
            return self.state.enqueue_or_append_provider_job(
                **arguments,
                appended_user_text="Fictional follow-up",
                quiet_ms=2000,
                max_ms=10000,
            )
        return self.state.enqueue_provider_job(**arguments)

    def notices(self, job_id: str):
        return self.state._connection.execute(
            "SELECT * FROM task_lifecycle_notices WHERE job_id=? ORDER BY created_at,kind",
            (job_id,),
        ).fetchall()

    def start(self, job, *, honor_stop: bool = True):
        lease = self.state.lease_provider_job("codex", "fictional-worker")
        assert lease is not None and lease.lease_token is not None
        self.assertEqual(lease.job_id, job.job_id)
        return self.state.mark_provider_job_executing(
            job.job_id,
            lease.lease_token,
            honor_stop=honor_stop,
        )

    def test_terminal_change_after_lease_supersedes_before_transport(self) -> None:
        job, _ = self.enqueue(10)
        now = datetime.now(timezone.utc)
        lease = self.state.task_notices.lease_notice("sender-a", now=now)
        assert lease is not None and lease.lease_token is not None
        # A separate state owner can terminalize directly, after notice leasing.
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='cancelled' WHERE job_id=?", (job.job_id,)
            )
        telegram = Mock()
        with patch.object(self.state.task_notices, "lease_notice", return_value=lease):
            result = deliver_task_notice(self.state.task_notices, telegram, "sender-a", now=now)
        self.assertTrue(result.worked)
        self.assertIsNone(result.error)
        telegram.send_html.assert_not_called()
        notice = self.state.task_notices.get_notice(lease.notice_id)
        self.assertEqual((notice.status, notice.attempt_count), ("superseded", 0))

    def capacity_reason(self, job_id: str) -> str:
        return next(
            row["event_key"].rsplit(":", 1)[1]
            for row in self.notices(job_id)
            if row["kind"] == "queued"
        )

    def test_capacity_reasons_distinguish_global_limit_from_provider_slots(self) -> None:
        first, _ = self.enqueue(10, enabled=False)
        self.start(first)
        for message, maximum, slots, expected in (
            (11, 1, 2, "global_capacity"),
            (12, 2, 1, "provider_slots"),
            (13, 2, 2, "worker_unknown"),
        ):
            topic, session = self.topic_session(f"example-project-{message}", message)
            job, _ = self.enqueue(
                message,
                topic=topic,
                session=session,
                capacity=QueueCapacityConfig(maximum, (), {"codex": slots}),
            )
            self.assertEqual(self.capacity_reason(job.job_id), expected)

    def test_capacity_snapshot_preserves_lower_fresh_worker_declaration(self) -> None:
        first, _ = self.enqueue(10, enabled=False)
        now = datetime.now(timezone.utc)
        lease = self.state.lease_provider_job(
            "codex",
            "codex-worker-2",
            max_parallel_roots=1,
            scheduler_agents=("codex",),
            agent_capacities={"codex": 2},
            now=now,
        )
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, lease.lease_token)
        topic, session = self.topic_session("example-project-other", 8)
        job, _ = self.enqueue(
            11,
            topic=topic,
            session=session,
            capacity=QueueCapacityConfig(3, ("codex",), {"codex": 2}),
        )
        self.assertEqual(self.capacity_reason(job.job_id), "global_capacity")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT declared_capacity FROM execution_scheduler_workers"
            ).fetchone()[0],
            1,
        )

    def test_expired_capacity_is_free_but_same_root_uncertainty_stays_blocked(self) -> None:
        first, _ = self.enqueue(10, enabled=False)
        self.start(first)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first.job_id),
            )
        capacity = QueueCapacityConfig(1, (), {"codex": 1})
        other, other_session = self.topic_session("example-project-other", 8)
        free, _ = self.enqueue(11, topic=other, session=other_session, capacity=capacity)
        self.assertEqual(self.capacity_reason(free.job_id), "worker_unknown")
        same, same_session = self.topic_session("example-project", 9)
        blocked, _ = self.enqueue(12, topic=same, session=same_session, capacity=capacity)
        self.assertEqual(self.capacity_reason(blocked.job_id), "root_active")

    def test_root_and_topic_fifo_reasons_precede_capacity(self) -> None:
        first, _ = self.enqueue(10, enabled=False)
        self.start(first)
        capacity = QueueCapacityConfig(1, (), {"codex": 1})
        queued, _ = self.enqueue(11, capacity=capacity)
        self.assertEqual(self.capacity_reason(queued.job_id), "topic_fifo")
        other, session = self.topic_session("example-project", 8)
        blocked, _ = self.enqueue(12, topic=other, session=session, capacity=capacity)
        self.assertEqual(self.capacity_reason(blocked.job_id), "root_active")

    def test_burst_append_does_not_replace_original_capacity_snapshot(self) -> None:
        capacity = QueueCapacityConfig(1, (), {"codex": 1})
        job, _ = self.enqueue(10, batch=True, capacity=capacity)
        before = [dict(row) for row in self.notices(job.job_id)]
        appended, _ = self.enqueue(
            11, batch=True, capacity=QueueCapacityConfig(3, (), {"codex": 3})
        )
        self.assertEqual(appended.job_id, job.job_id)
        self.assertEqual([dict(row) for row in self.notices(job.job_id)], before)

    def test_disabled_admission_and_execution_prepare_no_notices(self) -> None:
        job, _ = self.enqueue(10, enabled=False)
        self.start(job)
        self.assertEqual(self.notices(job.job_id), [])

    def test_acceptance_and_queue_snapshot_are_atomic_and_not_native_acceptance(self) -> None:
        job, created = self.enqueue(10)
        self.assertTrue(created)
        self.assertEqual(job.status, "queued")
        notices = self.notices(job.job_id)
        self.assertEqual({row["kind"] for row in notices}, {"accepted", "queued"})
        self.assertTrue(all(row["reply_to_message_id"] == 10 for row in notices))
        text = " ".join(row["telegram_html"] for row in notices)
        self.assertIn("not started", text)
        self.assertIn("unknown", text)
        self.assertNotIn("1 slot", text)

    def test_acceptance_failure_rolls_back_job_receipt_sequence_and_notices(self) -> None:
        original = self.state.task_notices.prepare_notice_in_transaction

        def fail_after_notice(**kwargs):
            original(**kwargs)
            raise RuntimeError("fictional notice fault")

        with patch.object(
            self.state.task_notices, "prepare_notice_in_transaction", fail_after_notice
        ):
            with self.assertRaisesRegex(RuntimeError, "notice fault"):
                self.enqueue(10)
        self.assertEqual(self.state.provider_jobs_for_topic(self.topic.topic_id), ())
        self.assertFalse(self.state.message_already_observed(self.topic.chat_id, 10))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            0,
        )
        job, _ = self.enqueue(10)
        self.assertEqual(job.topic_sequence, 1)

    def test_duplicate_admission_keeps_exact_notice_content_after_state_changes(self) -> None:
        job, _ = self.enqueue(10)
        before = [(row["notice_id"], row["telegram_html"]) for row in self.notices(job.job_id)]
        self.start(job)
        duplicate, created = self.enqueue(10)
        self.assertFalse(created)
        self.assertEqual(duplicate.job_id, job.job_id)
        retained = {row["notice_id"]: row["telegram_html"] for row in self.notices(job.job_id)}
        self.assertTrue(all(retained[notice_id] == text for notice_id, text in before))

    def test_batched_input_has_one_acceptance_and_no_retroactive_opt_in(self) -> None:
        job, _ = self.enqueue(10, batch=True)
        appended, created = self.enqueue(11, batch=True)
        self.assertTrue(created)
        self.assertEqual(appended.job_id, job.job_id)
        self.assertEqual(len(self.notices(job.job_id)), 2)
        self.state.cancel_provider_job(job.job_id)
        disabled, _ = self.enqueue(20, batch=True, enabled=False)
        appended, _ = self.enqueue(21, batch=True)
        self.assertEqual(disabled.job_id, appended.job_id)
        self.assertEqual(self.notices(disabled.job_id), [])

    def test_fifo_snapshot_includes_same_topic_result_delivery_boundary(self) -> None:
        earlier, _ = self.enqueue(10, enabled=False)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='result_ready' WHERE job_id=?", (earlier.job_id,)
            )
        job, _ = self.enqueue(11)
        queued = next(row for row in self.notices(job.job_id) if row["kind"] == "queued")
        self.assertIn("topic_fifo", queued["event_key"])
        self.assertIn("delivery", queued["telegram_html"])

    def test_active_root_snapshot_uses_numeric_owner_topic_link(self) -> None:
        earlier, _ = self.enqueue(10, enabled=False)
        self.start(earlier)
        topic, session = self.topic_session("example-project", 8)
        job, _ = self.enqueue(11, topic=topic, session=session)
        queued = next(row for row in self.notices(job.job_id) if row["kind"] == "queued")
        self.assertIn("root_active", queued["event_key"])
        self.assertIn("https://t.me/c/1234567890/7", queued["telegram_html"])
        self.assertIn("/stop", queued["telegram_html"])
        self.assertIsNone(
            self.state.lease_provider_job("codex", "other-worker", max_parallel_roots=2)
        )

    def test_collection_deadline_is_not_reported_as_provider_capacity(self) -> None:
        job, _ = self.enqueue(10, batch=True)
        queued = next(row for row in self.notices(job.job_id) if row["kind"] == "queued")
        self.assertIn("collecting", queued["event_key"])
        self.assertIn("messages", queued["telegram_html"])

    def test_execution_supersedes_only_unattempted_transient_notices(self) -> None:
        job, _ = self.enqueue(10)
        now = datetime.now(timezone.utc)
        lease = self.state.task_notices.lease_notice("sender-a", now=now)
        assert lease is not None and lease.lease_token is not None
        self.state.task_notices.begin_send(lease.notice_id, lease.lease_token, now=now)
        executing = self.start(job)
        rows = self.notices(job.job_id)
        attempted = next(row for row in rows if row["notice_id"] == lease.notice_id)
        self.assertEqual(attempted["status"], "leased")
        execution = next(row for row in rows if row["kind"] == "executing")
        self.assertIn("handoff", execution["telegram_html"])
        self.assertIn("not yet confirmed", execution["telegram_html"])
        self.assertIn(str(executing.attempt_count), execution["event_key"])
        self.assertEqual(sum(row["status"] == "superseded" for row in rows), 1)

    def test_unattempted_sender_lease_is_superseded_before_network_send(self) -> None:
        job, _ = self.enqueue(10)
        now = datetime.now(timezone.utc)
        lease = self.state.task_notices.lease_notice("sender-a", now=now)
        assert lease is not None and lease.lease_token is not None
        self.start(job)
        result = self.state.task_notices.begin_send(lease.notice_id, lease.lease_token, now=now)
        self.assertEqual((result.status, result.attempt_count), ("superseded", 0))

    def test_stop_before_start_has_no_execution_notice_and_supersedes_queue(self) -> None:
        job, _ = self.enqueue(10)
        lease = self.state.lease_provider_job("codex", "fictional-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=11,
            target_agent_id="codex",
            prepare_notice=True,
        )
        cancelled = self.state.mark_provider_job_executing(
            job.job_id,
            lease.lease_token,
            honor_stop=True,
        )
        self.assertEqual(cancelled.status, "cancelled")
        rows = self.notices(job.job_id)
        self.assertNotIn("executing", {row["kind"] for row in rows})
        self.assertTrue(
            all(
                row["status"] == "superseded"
                for row in rows
                if row["kind"] in {"accepted", "queued"}
            )
        )

    def test_execution_notice_fault_rolls_back_start_and_supersession(self) -> None:
        job, _ = self.enqueue(10)
        original = self.state.task_notices.prepare_notice_in_transaction

        def fail_execution(**kwargs):
            result = original(**kwargs)
            if kwargs["kind"] == "executing":
                raise RuntimeError("fictional executing notice fault")
            return result

        with patch.object(self.state.task_notices, "prepare_notice_in_transaction", fail_execution):
            with self.assertRaisesRegex(RuntimeError, "executing notice fault"):
                self.start(job)
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "leased")
        self.assertEqual({row["status"] for row in self.notices(job.job_id)}, {"pending"})

    def test_unknown_delivery_history_survives_terminal_uncertainty_and_restart(self) -> None:
        job, _ = self.enqueue(10)
        now = datetime.now(timezone.utc)
        lease = self.state.task_notices.lease_notice("sender-a", now=now)
        assert lease is not None and lease.lease_token is not None
        self.state.task_notices.begin_send(lease.notice_id, lease.lease_token, now=now)
        self.state.task_notices.mark_send_unknown(
            lease.notice_id,
            lease.lease_token,
            error_code="fictional_unknown",
            now=now,
        )
        executing = self.start(job)
        assert executing.lease_token is not None
        self.state.mark_provider_job_indeterminate(
            job.job_id,
            executing.lease_token,
            error_code="fictional_outcome",
        )
        self.state.close()
        self.state = HubState.open_existing(self.path, codex_permission_profile=None)
        rows = self.notices(job.job_id)
        self.assertEqual(
            next(row for row in rows if row["notice_id"] == lease.notice_id)["status"], "unknown"
        )
        self.assertTrue(all(row["status"] in {"unknown", "superseded"} for row in rows))
        topic, session = self.topic_session("example-project", 8)
        with self.assertRaisesRegex(Exception, "persistent local writer or uncertainty"):
            self.enqueue(20, topic=topic, session=session)

    def test_terminal_failure_supersedes_handoff_inside_failure_notice_commit(self) -> None:
        job, _ = self.enqueue(10)
        executing = self.start(job)
        assert executing.lease_token is not None
        self.state.terminate_provider_job_with_notice(
            job.job_id,
            executing.lease_token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="fictional_failure",
            sender_agent_id="codex",
            telegram_html="Outcome unknown; root remains paused.",
        )
        self.assertEqual({row["status"] for row in self.notices(job.job_id)}, {"superseded"})

    def test_successful_result_supersedes_handoff_atomically(self) -> None:
        job, _ = self.enqueue(10)
        executing = self.start(job)
        assert executing.lease_token is not None
        self.state.commit_provider_result(
            job.job_id,
            executing.lease_token,
            visible_response="Fictional completion",
            sender_agent_id="codex",
            telegram_html="Fictional completion",
        )
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "result_ready")
        self.assertEqual({row["status"] for row in self.notices(job.job_id)}, {"superseded"})

    def test_stale_execution_recovery_supersedes_handoff_without_releasing_root(self) -> None:
        job, _ = self.enqueue(10)
        now = datetime.now(timezone.utc)
        lease = self.state.lease_provider_job("codex", "fictional-worker", now=now, lease_seconds=1)
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token, now=now)
        recovery = self.state.recover_stale_provider_jobs(now=now + timedelta(seconds=2))
        self.assertEqual(recovery.indeterminate_job_ids, (job.job_id,))
        self.assertEqual({row["status"] for row in self.notices(job.job_id)}, {"superseded"})
        topic, session = self.topic_session("example-project", 8)
        with self.assertRaisesRegex(Exception, "persistent local writer or uncertainty"):
            self.enqueue(20, topic=topic, session=session)

    def test_stop_cancels_unstarted_jobs_and_supersedes_their_snapshots(self) -> None:
        job, _ = self.enqueue(10)
        self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=11,
            target_agent_id="codex",
            prepare_notice=True,
        )
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "cancelled")
        self.assertTrue(all(row["status"] == "superseded" for row in self.notices(job.job_id)))

    def test_same_reason_never_repeats_or_rewrites_owner_snapshot(self) -> None:
        earlier, _ = self.enqueue(10, enabled=False)
        self.start(earlier)
        topic, session = self.topic_session("example-project", 8)
        job, _ = self.enqueue(11, topic=topic, session=session)
        before = [(row["notice_id"], row["telegram_html"]) for row in self.notices(job.job_id)]
        with self.state._immediate_transaction():
            self.state.queue_visibility.queued_in_transaction(
                job.job_id, now=datetime.now(timezone.utc)
            )
        self.assertEqual(
            before, [(row["notice_id"], row["telegram_html"]) for row in self.notices(job.job_id)]
        )

    def test_rejected_send_attempt_history_is_preserved_after_execution(self) -> None:
        job, _ = self.enqueue(10)
        now = datetime.now(timezone.utc)
        lease = self.state.task_notices.lease_notice("sender-a", now=now)
        assert lease is not None and lease.lease_token is not None
        self.state.task_notices.begin_send(lease.notice_id, lease.lease_token, now=now)
        self.state.task_notices.retry_rejected(
            lease.notice_id,
            lease.lease_token,
            error_code="fictional_rejection",
            available_at=now + timedelta(seconds=30),
            now=now,
        )
        self.start(job)
        notice = self.state.task_notices.get_notice(lease.notice_id)
        self.assertEqual((notice.status, notice.attempt_count), ("pending", 1))
        self.assertEqual(notice.error_code, "fictional_rejection")


if __name__ == "__main__":
    unittest.main()
