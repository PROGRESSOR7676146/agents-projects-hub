from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.codex_activity import CodexActivityEvent, CodexActivityKind
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.preacceptance_approvals import PreacceptanceApprovalState
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.task_activity import TaskActivityState
from hermes_codex_router.task_notice_sender import deliver_task_notice
from tests.test_outbox_sender import Bot


class PreacceptanceApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = HubState.open(self.root / "state.db", codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.now = datetime.now(timezone.utc)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
            execution_root=self.root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "codex", "example-model", "high"
        )
        self.job, _ = self.state.enqueue_provider_job(
            idempotency_key="example-input",
            chat_id=self.topic.chat_id,
            message_id=1,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Example task",
        )
        lease = self.state.lease_provider_job("codex", "example-worker", now=self.now)
        assert lease is not None and lease.lease_token is not None
        self.token = lease.lease_token
        self.state.mark_provider_job_executing(self.job.job_id, self.token)
        self.journal = ExecutionJournal(self.state)
        self.journal.record_thread(
            self.job.job_id, self.token, "example-thread", self.root, codex_permission_profile=None
        )
        self.early = PreacceptanceApprovalState(self.state, notices_enabled=True)
        self.runtime = self.early.register_runtime(
            agent_id="codex", worker_slot=1, instance_token="example-instance-a", now=self.now
        )
        self.activity = TaskActivityState(
            self.state._connection,
            transaction=self.state._immediate_transaction,
            state_error=StateError,
            notices=self.state.task_notices,
            notices_enabled=True,
        )

    def scope(self, runtime=None):
        return self.early.open_scope(
            self.job.job_id,
            self.token,
            self.runtime if runtime is None else runtime,
            provider_thread_id="example-thread",
            project_root=str(self.root),
            now=self.now,
        )

    def event(
        self,
        kind: CodexActivityKind = "approval_requested",
        request: str | int = 7,
        turn="example-turn",
    ):
        return CodexActivityEvent(kind, "command", "example-thread", turn, "example-item", request)

    def observe(
        self,
        scope,
        *,
        kind: CodexActivityKind = "approval_requested",
        request: str | int = 7,
        turn="example-turn",
    ):
        return self.early.observe(
            scope, self.runtime, self.event(kind, request, turn), now=self.now
        )

    def promote(self, scope, turn="example-turn"):
        with self.state._immediate_transaction():
            self.activity.bind_accepted_in_transaction(
                self.job.job_id, self.token, "example-thread", turn, str(self.root), now=self.now
            )
            return self.early.promote_in_transaction(
                scope,
                self.runtime,
                self.activity,
                thread_id="example-thread",
                turn_id=turn,
                now=self.now,
            )

    def notice(self):
        return self.state._connection.execute("SELECT * FROM task_lifecycle_notices").fetchone()

    def next_prepared_job(self):
        root = self.root / "next-project"
        root.mkdir()
        topic = self.state.observe_topic(
            project_id="example-next",
            chat_id=self.topic.chat_id,
            thread_id=78,
            title="Example next",
            execution_root=root,
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "example-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="example-next-input",
            chat_id=topic.chat_id,
            message_id=2,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Example next task",
        )
        lease = self.state.lease_provider_job(
            "codex",
            "example-next-worker",
            now=self.now,
            max_parallel_roots=2,
            agent_capacities={"codex": 2},
        )
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        self.journal.record_thread(job.job_id, lease.lease_token, "example-next-thread", root)
        return job, lease.lease_token, root

    def open_next_scope(self, job, token, root):
        return self.early.open_scope(
            job.job_id,
            token,
            self.runtime,
            provider_thread_id="example-next-thread",
            project_root=str(root),
            now=self.now,
        )

    def test_same_instance_retires_abandoned_scope_before_next_job(self) -> None:
        old = self.scope()
        self.observe(old)
        self.state.cancel_active_provider_job(self.job.job_id, self.token)
        next_job, token, root = self.next_prepared_job()
        new = self.open_next_scope(next_job, token, root)
        self.assertIsNotNone(new)
        self.assertNotEqual(new, old)
        self.assertEqual(self.notice()["status"], "superseded")
        row = self.state._connection.execute(
            "SELECT state FROM preacceptance_scopes WHERE scope_id=?", (old,)
        ).fetchone()
        self.assertEqual(row["state"], "retired")
        self.assertEqual(self.open_next_scope(next_job, token, root), new)
        self.assertEqual(self.runtime.epoch, 1)

    def test_same_instance_preserves_other_valid_live_scope(self) -> None:
        old = self.scope()
        self.observe(old)
        next_job, token, root = self.next_prepared_job()
        self.assertIsNone(self.open_next_scope(next_job, token, root))
        self.assertEqual(self.scope(), old)
        self.assertEqual(self.notice()["status"], "pending")

    def test_replacement_insert_failure_rolls_back_retirement_and_suppression(self) -> None:
        old = self.scope()
        self.observe(old)
        self.state.cancel_active_provider_job(self.job.job_id, self.token)
        next_job, token, root = self.next_prepared_job()
        with self.state._connection:
            self.state._connection.execute(
                "CREATE TRIGGER example_scope_insert_failure BEFORE INSERT ON preacceptance_scopes "
                "BEGIN SELECT RAISE(ABORT,'example failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.open_next_scope(next_job, token, root)
        row = self.state._connection.execute(
            "SELECT state FROM preacceptance_scopes WHERE scope_id=?", (old,)
        ).fetchone()
        self.assertEqual(row["state"], "open")
        self.assertEqual(self.notice()["status"], "pending")

    def test_pending_covering_stop_suppresses_unattempted_early_notice(self) -> None:
        scope = self.scope()
        self.observe(scope)
        self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=3,
            target_agent_id="codex",
        )
        bot = Bot()
        deliver_task_notice(self.state.task_notices, bot, "example-sender", now=self.now)
        self.assertEqual(bot.sent, [])
        self.assertEqual(self.notice()["status"], "superseded")
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")

    def test_pending_covering_stop_suppresses_accepted_approval_notice(self) -> None:
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.activity.bind_accepted(
            self.job.job_id,
            self.token,
            "example-thread",
            "example-turn",
            str(self.root),
            now=self.now,
        )
        self.activity.record_activity(self.job.job_id, self.token, self.event(), now=self.now)
        self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=3,
            target_agent_id="codex",
        )
        bot = Bot()
        deliver_task_notice(self.state.task_notices, bot, "example-sender", now=self.now)
        self.assertEqual(bot.sent, [])
        self.assertEqual(self.notice()["status"], "superseded")

    def test_pending_stop_preserves_already_attempted_unknown_notice(self) -> None:
        scope = self.scope()
        self.observe(scope)
        notices = self.state.task_notices
        notice = notices.lease_notice("example-sender", now=self.now)
        assert notice is not None and notice.lease_token is not None
        notices.begin_send(notice.notice_id, notice.lease_token, now=self.now)
        self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=3,
            target_agent_id="codex",
        )
        notices.mark_send_unknown(
            notice.notice_id, notice.lease_token, error_code="example_timeout", now=self.now
        )
        self.early.retire(scope, self.runtime, now=self.now)
        self.assertEqual(self.notice()["status"], "unknown")
        self.assertEqual(self.notice()["attempt_count"], 1)

    def test_conflicting_instance_scope_is_preserved(self) -> None:
        scope = self.scope()
        self.observe(scope)
        next_job, token, root = self.next_prepared_job()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE preacceptance_scopes SET instance_token='example-other-instance'"
            )
        self.assertIsNone(self.open_next_scope(next_job, token, root))
        row = self.state._connection.execute(
            "SELECT state FROM preacceptance_scopes WHERE scope_id=?", (scope,)
        ).fetchone()
        self.assertEqual(row["state"], "open")
        self.assertEqual(self.notice()["status"], "pending")

    def test_promotion_and_delivery_with_advanced_clock_preserve_pending_notice(self) -> None:
        scope = self.scope()
        self.observe(scope)
        notice_id = self.notice()["notice_id"]
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.now += timedelta(seconds=5)
        self.assertTrue(self.promote(scope))
        bot = Bot()
        result = deliver_task_notice(
            self.state.task_notices, bot, "example-sender", now=self.now + timedelta(seconds=1)
        )
        self.assertTrue(result.delivered)
        self.assertEqual(self.notice()["notice_id"], notice_id)
        self.assertEqual(len(bot.sent), 1)

    def test_approval_is_delivered_before_turn_acceptance_without_execution_authority(self) -> None:
        scope = self.scope()
        self.assertTrue(self.observe(scope))
        checkpoint = self.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(
            self.state._connection.execute("SELECT count(*) FROM task_activity").fetchone()[0], 0
        )
        bot = Bot()
        outcome = deliver_task_notice(self.state.task_notices, bot, "example-sender", now=self.now)
        self.assertTrue(outcome.delivered)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Codex session", bot.sent[0][2])
        self.assertNotIn("/stop", bot.sent[0][2])
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")
        self.assertEqual(self.state.get_session(self.session.session_id).writer_mode, "telegram")

    def test_early_resolution_supersedes_only_an_unattempted_notice(self) -> None:
        scope = self.scope()
        self.assertTrue(self.observe(scope))
        self.assertTrue(self.observe(scope, kind="approval_resolved"))
        bot = Bot()
        deliver_task_notice(self.state.task_notices, bot, "example-sender", now=self.now)
        self.assertEqual(bot.sent, [])
        self.assertEqual(self.notice()["status"], "superseded")

    def test_typed_request_ids_are_distinct_and_duplicates_do_not_add_notices(self) -> None:
        scope = self.scope()
        self.assertFalse(self.observe(scope, kind="approval_resolved", request=99))
        self.assertTrue(self.observe(scope, request=7))
        self.assertTrue(self.observe(scope, request="7"))
        self.assertFalse(self.observe(scope, request=7))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            2,
        )
        with self.assertRaises(StateError):
            self.observe(scope, request=7, turn="example-other-turn")

    def test_old_epoch_cannot_create_or_record_and_other_slots_do_not_retire_it(self) -> None:
        scope = self.scope()
        self.early.register_runtime(
            agent_id="codex", worker_slot=2, instance_token="example-other-slot", now=self.now
        )
        self.assertTrue(self.observe(scope))
        newer = self.early.register_runtime(
            agent_id="codex", worker_slot=1, instance_token="example-instance-b", now=self.now
        )
        self.assertGreater(newer.epoch, self.runtime.epoch)
        self.assertIsNone(self.scope())
        self.assertFalse(self.observe(scope, request=8))
        self.assertEqual(self.notice()["status"], "superseded")
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.assertFalse(self.promote(scope))
        self.assertEqual(
            self.state._connection.execute("SELECT count(*) FROM task_activity_entries").fetchone()[
                0
            ],
            0,
        )
        self.assertEqual(self.state.get_provider_job(self.job.job_id).lease_token, self.token)

    def test_epoch_registration_rolls_back_retirement_and_counter(self) -> None:
        scope = self.scope()
        self.observe(scope)
        with self.state._connection:
            self.state._connection.execute(
                "CREATE TRIGGER example_epoch_failure BEFORE UPDATE ON preacceptance_runtime_epochs "
                "BEGIN SELECT RAISE(ABORT,'example epoch failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.early.register_runtime(
                agent_id="codex", worker_slot=1, instance_token="example-instance-b", now=self.now
            )
        self.assertTrue(self.observe(scope, request=8))
        self.assertEqual(self.notice()["status"], "pending")

    def test_acceptance_gap_preserves_matching_notice_and_promotion_deduplicates(self) -> None:
        scope = self.scope()
        self.observe(scope)
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        bot = Bot()
        self.assertTrue(
            deliver_task_notice(
                self.state.task_notices, bot, "example-sender", now=self.now
            ).delivered
        )
        self.assertTrue(self.promote(scope))
        self.assertFalse(
            self.activity.record_activity(self.job.job_id, self.token, self.event(), now=self.now)
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.state._connection.execute("SELECT count(*) FROM task_activity_entries").fetchone()[
                0
            ],
            1,
        )

    def test_same_thread_foreign_turn_never_becomes_accepted_activity(self) -> None:
        scope = self.scope()
        self.observe(scope, turn="example-foreign-turn")
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.assertTrue(self.promote(scope))
        self.assertEqual(
            self.state._connection.execute("SELECT count(*) FROM task_activity_entries").fetchone()[
                0
            ],
            0,
        )
        self.assertEqual(self.notice()["status"], "superseded")

    def test_epoch_change_after_send_keeps_positive_receipt_evidence(self) -> None:
        scope = self.scope()
        self.observe(scope)
        notice = self.state.task_notices.lease_notice("example-sender", now=self.now)
        assert notice is not None and notice.lease_token is not None
        attempt = self.state.task_notices.begin_send(
            notice.notice_id, notice.lease_token, now=self.now
        )
        self.early.register_runtime(
            agent_id="codex", worker_slot=1, instance_token="example-instance-b", now=self.now
        )
        self.state.task_notices.complete_send(
            attempt.notice_id, notice.lease_token, telegram_message_id=101, now=self.now
        )
        self.assertEqual(self.notice()["status"], "delivered")

    def test_missing_or_changed_profile_context_cannot_authorize_early_send(self) -> None:
        scope = self.scope()
        for index, context in enumerate(({}, {"codex_permission_profile": "example-policy"})):
            self.observe(scope, request=20 + index)
            with (
                self.subTest(context=context),
                closing(HubState.open(self.root / "state.db", **context)) as sender,
            ):
                bot = Bot()
                deliver_task_notice(sender.task_notices, bot, "example-sender", now=self.now)
                self.assertEqual(bot.sent, [])

    def test_expired_lease_cannot_authorize_a_first_send(self) -> None:
        scope = self.scope()
        self.observe(scope)
        bot = Bot()
        deliver_task_notice(
            self.state.task_notices, bot, "example-sender", now=self.now + timedelta(hours=1)
        )
        self.assertEqual(bot.sent, [])

    def test_late_old_instance_cannot_open_after_new_instance_but_current_can(self) -> None:
        newer = self.early.register_runtime(
            agent_id="codex", worker_slot=1, instance_token="example-instance-b", now=self.now
        )
        self.assertIsNone(self.scope())
        self.assertIsNotNone(self.scope(newer))

    def test_concurrent_startup_serializes_epoch_and_fences_the_loser(self) -> None:
        barrier = threading.Barrier(2)

        def register(instance):
            with closing(
                HubState.open(self.root / "state.db", codex_permission_profile=None)
            ) as state:
                barrier.wait(timeout=3)
                return PreacceptanceApprovalState(state).register_runtime(
                    agent_id="codex", worker_slot=1, instance_token=instance, now=self.now
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            epochs = list(pool.map(register, ("example-instance-b", "example-instance-c")))
        self.assertEqual(sorted(epoch.epoch for epoch in epochs), [2, 3])
        loser, winner = sorted(epochs, key=lambda epoch: epoch.epoch)
        self.assertIsNone(self.scope(loser))
        self.assertIsNotNone(self.scope(winner))

    def test_binding_changes_suppress_early_send(self) -> None:
        mutations = (
            "UPDATE provider_jobs SET lease_token='example-other-lease'",
            "UPDATE agent_sessions SET generation=generation+1",
            "UPDATE agent_sessions SET writer_mode='local'",
            "UPDATE agent_sessions SET status='archived'",
            "UPDATE agent_sessions SET provider_session_id='example-other-thread'",
            "UPDATE topics SET chat_id=-1001111111111",
            "UPDATE topics SET thread_id=78",
            "UPDATE topics SET execution_scope='root:/home/example/other'",
            "UPDATE provider_execution_checkpoints SET project_root='/home/example/other'",
            "UPDATE provider_execution_checkpoints SET completed_text='Example complete'",
            "UPDATE provider_execution_checkpoints SET provider_turn_id='example-other-turn'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                fixture = PreacceptanceApprovalTests()
                fixture.setUp()
                try:
                    scope = fixture.scope()
                    fixture.observe(scope)
                    with fixture.state._connection:
                        fixture.state._connection.execute(mutation)
                    bot = Bot()
                    deliver_task_notice(
                        fixture.state.task_notices, bot, "example-sender", now=fixture.now
                    )
                    self.assertEqual(bot.sent, [])
                    self.assertEqual(fixture.notice()["status"], "superseded")
                finally:
                    fixture.doCleanups()

    def test_request_resolution_before_acceptance_imports_resolved_without_resurrection(
        self,
    ) -> None:
        scope = self.scope()
        self.observe(scope)
        self.observe(scope, kind="approval_resolved")
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.promote(scope)
        self.assertFalse(
            self.activity.record_activity(self.job.job_id, self.token, self.event(), now=self.now)
        )
        self.assertEqual(
            self.state._connection.execute("SELECT state FROM task_activity_entries").fetchone()[0],
            "resolved",
        )
        self.assertEqual(
            self.state._connection.execute("SELECT mode FROM task_activity").fetchone()[0],
            "ordinary",
        )
        self.assertEqual(self.notice()["status"], "superseded")

    def test_combined_physical_bound_and_resolution_at_both_limits(self) -> None:
        scope = self.scope()
        for request in range(128):
            self.assertTrue(self.observe(scope, request=request))
        self.assertFalse(self.observe(scope, request=128))
        self.assertTrue(self.observe(scope, kind="approval_resolved", request=0))
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.promote(scope)
        for item in range(256):
            self.assertTrue(
                self.activity.record_activity(
                    self.job.job_id,
                    self.token,
                    CodexActivityEvent(
                        "tool_started",
                        "command",
                        "example-thread",
                        "example-turn",
                        f"example-tool-{item}",
                    ),
                    now=self.now,
                )
            )
        self.assertFalse(
            self.activity.record_activity(
                self.job.job_id,
                self.token,
                CodexActivityEvent(
                    "tool_started", "command", "example-thread", "example-turn", "example-overflow"
                ),
                now=self.now,
            )
        )
        self.assertTrue(
            self.activity.record_activity(
                self.job.job_id,
                self.token,
                self.event("approval_resolved", request=1),
                now=self.now,
            )
        )
        counts = [
            self.state._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("preacceptance_requests", "task_activity_entries")
        ]
        self.assertEqual(sum(counts), 512)

    def test_epoch_change_keeps_unknown_and_proven_rejection_transport_semantics(self) -> None:
        for outcome in ("unknown", "rejected"):
            with self.subTest(outcome=outcome):
                fixture = PreacceptanceApprovalTests()
                fixture.setUp()
                try:
                    scope = fixture.scope()
                    fixture.observe(scope)
                    notices = fixture.state.task_notices
                    notice = notices.lease_notice("example-sender", now=fixture.now)
                    assert notice is not None and notice.lease_token is not None
                    notices.begin_send(notice.notice_id, notice.lease_token, now=fixture.now)
                    fixture.early.register_runtime(
                        agent_id="codex",
                        worker_slot=1,
                        instance_token="example-instance-b",
                        now=fixture.now,
                    )
                    if outcome == "unknown":
                        notices.mark_send_unknown(
                            notice.notice_id,
                            notice.lease_token,
                            error_code="example_timeout",
                            now=fixture.now,
                        )
                        notices.recover_expired_notices(now=fixture.now + timedelta(hours=1))
                        self.assertIsNone(
                            notices.lease_notice(
                                "example-sender", now=fixture.now + timedelta(hours=1)
                            )
                        )
                        self.assertEqual(fixture.notice()["status"], "unknown")
                    else:
                        notices.retry_rejected(
                            notice.notice_id,
                            notice.lease_token,
                            error_code="example_rejection",
                            available_at=fixture.now,
                            now=fixture.now,
                        )
                        bot = Bot()
                        self.assertTrue(
                            deliver_task_notice(
                                notices, bot, "example-sender", now=fixture.now
                            ).delivered
                        )
                        self.assertEqual(fixture.notice()["attempt_count"], 2)
                finally:
                    fixture.doCleanups()

    def test_retired_provenance_cannot_fall_through_to_an_accepted_alias(self) -> None:
        scope = self.scope()
        self.observe(scope, turn="example-foreign-turn")
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.promote(scope)
        self.assertTrue(
            self.activity.record_activity(self.job.job_id, self.token, self.event(), now=self.now)
        )
        with self.state._connection:
            self.state._connection.execute("UPDATE task_lifecycle_notices SET status='pending'")
        bot = Bot()
        deliver_task_notice(self.state.task_notices, bot, "example-sender", now=self.now)
        self.assertEqual(bot.sent, [])
        self.assertEqual(self.notice()["status"], "superseded")

    def test_promoted_pending_notice_survives_epoch_rollover(self) -> None:
        scope = self.scope()
        self.observe(scope)
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.promote(scope)
        self.early.register_runtime(
            agent_id="codex", worker_slot=1, instance_token="example-instance-b", now=self.now
        )
        bot = Bot()
        self.assertTrue(
            deliver_task_notice(
                self.state.task_notices, bot, "example-sender", now=self.now
            ).delivered
        )
        self.assertEqual(len(bot.sent), 1)

    def test_promoted_notice_requires_exact_accepted_request_metadata(self) -> None:
        mutations = (
            "UPDATE task_activity_entries SET item_identity='example-other-item'",
            "UPDATE task_activity_entries SET category='network'",
            "UPDATE task_activity_entries SET state='resolved'",
            "UPDATE provider_execution_checkpoints SET provider_turn_id='example-other-turn'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                fixture = PreacceptanceApprovalTests()
                fixture.setUp()
                try:
                    scope = fixture.scope()
                    fixture.observe(scope)
                    fixture.journal.record_turn(fixture.job.job_id, fixture.token, "example-turn")
                    fixture.promote(scope)
                    with fixture.state._connection:
                        fixture.state._connection.execute(mutation)
                    bot = Bot()
                    deliver_task_notice(
                        fixture.state.task_notices, bot, "example-sender", now=fixture.now
                    )
                    self.assertEqual(bot.sent, [])
                    self.assertEqual(fixture.notice()["status"], "superseded")
                finally:
                    fixture.doCleanups()

        scope = self.scope()
        self.observe(scope)
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        self.promote(scope)
        with closing(
            HubState.open(self.root / "state.db", codex_permission_profile="example-policy")
        ) as changed_profile:
            bot = Bot()
            deliver_task_notice(changed_profile.task_notices, bot, "example-sender", now=self.now)
            self.assertEqual(bot.sent, [])
            self.assertEqual(self.notice()["status"], "superseded")

    def test_scope_opening_requires_exact_prepared_context(self) -> None:
        for changes in (
            {"provider_thread_id": "example-other-thread"},
            {"project_root": "/home/example/other"},
            {"token": "example-other-lease"},
        ):
            with self.subTest(changes=changes), self.assertRaises(StateError):
                self.early.open_scope(
                    self.job.job_id,
                    changes.get("token", self.token),
                    self.runtime,
                    provider_thread_id=changes.get("provider_thread_id", "example-thread"),
                    project_root=changes.get("project_root", str(self.root)),
                    now=self.now,
                )
        with closing(HubState.open(self.root / "state.db")) as missing_context:
            with self.assertRaises(StateError):
                PreacceptanceApprovalState(missing_context).open_scope(
                    self.job.job_id,
                    self.token,
                    self.runtime,
                    provider_thread_id="example-thread",
                    project_root=str(self.root),
                    now=self.now,
                )
        self.journal.record_turn(self.job.job_id, self.token, "example-turn")
        with self.assertRaises(StateError):
            self.scope()

    def test_wrong_thread_or_malformed_metadata_cannot_create_early_observations(self) -> None:
        scope = self.scope()
        self.assertFalse(
            self.early.observe(
                scope,
                self.runtime,
                replace(self.event(), thread_id="example-other-thread"),
                now=self.now,
            )
        )
        for event in (
            replace(self.event(), request_id=True),
            replace(self.event(), request_id=2**63),
            replace(self.event(), item_id="/home/example/private"),
            replace(self.event(), turn_id="bad turn"),
            replace(self.event(), kind="tool_started"),
        ):
            with self.subTest(event=event), self.assertRaises(StateError):
                self.early.observe(scope, self.runtime, event, now=self.now)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM preacceptance_requests"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.notice())


if __name__ == "__main__":
    unittest.main()
