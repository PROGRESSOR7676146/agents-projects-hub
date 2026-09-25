from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.controller_admission import (
    DuplicateAdmission,
    DurableAdmissionRequest,
    DurableProviderAdmission,
    RejectedAdmission,
)
from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.telegram import TopicMessage
from tests.fault_matrix_support import FaultMatrixHarness, RecordingAdapter


class ExecutionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.state = HubState.open(self.base / "state.db")

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def topic_session(
        self,
        *,
        project_id: str,
        thread_id: int,
        agent_id: str,
        root: Path | None = None,
    ):
        topic_args = {
            "project_id": project_id,
            "chat_id": -1001234567890,
            "thread_id": thread_id,
            "title": f"Fictional topic {thread_id}",
        }
        if root is not None:
            topic_args["execution_root"] = root
        topic = self.state.observe_topic(  # type: ignore[arg-type]
            **topic_args,
        )
        session = self.state.activate_agent(
            topic.topic_id,
            agent_id,
            "fictional-model",
            "high",
        )
        return topic, session

    def enqueue(self, topic, session, message_id: int):
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"telegram:{topic.chat_id}:{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text=f"fictional request {message_id}",
        )
        return job

    def test_same_root_jobs_across_topics_and_providers_are_serialized(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project",
            thread_id=71,
            agent_id="opencode",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project",
            thread_id=72,
            agent_id="antigravity",
        )
        first = self.enqueue(first_topic, first_session, 701)
        second = self.enqueue(second_topic, second_session, 702)

        leased = self.state.lease_provider_job("opencode", "opencode-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)

        self.assertIsNone(self.state.lease_provider_job("antigravity", "agy-worker"))
        self.state.fail_provider_job(
            first.job_id,
            leased.lease_token,
            error_class="fictional",
            error_code="fictional_done",
        )
        released = self.state.lease_provider_job("antigravity", "agy-worker")
        self.assertIsNotNone(released)
        assert released is not None
        self.assertEqual(released.job_id, second.job_id)

    def test_local_writer_in_another_topic_blocks_the_same_root(self) -> None:
        local_topic, local_session = self.topic_session(
            project_id="example-project",
            thread_id=73,
            agent_id="codex",
        )
        work_topic, work_session = self.topic_session(
            project_id="example-project",
            thread_id=74,
            agent_id="opencode",
        )
        self.state.set_writer_mode(local_session.session_id, "local")
        with self.assertRaisesRegex(StateError, "persistent local writer"):
            self.enqueue(work_topic, work_session, 703)

        self.assertIsNone(self.state.lease_provider_job("opencode", "opencode-worker"))
        self.state.set_writer_mode(local_session.session_id, "telegram")
        self.assertIsNone(self.state.lease_provider_job("opencode", "opencode-worker"))
        self.assertEqual(local_topic.execution_scope, work_topic.execution_scope)

    def test_new_request_is_not_queued_behind_another_topics_local_writer(self) -> None:
        _, owner = self.topic_session(project_id="example-project", thread_id=75, agent_id="codex")
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=76, agent_id="codex"
        )
        self.state.set_writer_mode(owner.session_id, "local")

        with self.assertRaisesRegex(StateError, "local writer"):
            self.enqueue(destination, selected, 704)

        self.assertEqual(self.state.provider_jobs_for_topic(destination.topic_id), ())

    def test_blocked_input_has_one_durable_hub_notice_per_telegram_message(self) -> None:
        owner_topic, owner = self.topic_session(
            project_id="example-project", thread_id=77, agent_id="codex"
        )
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=78, agent_id="codex"
        )
        self.state.set_writer_mode(owner.session_id, "local")

        first = self.state.reject_blocked_provider_input(
            chat_id=destination.chat_id,
            message_id=705,
            topic_id=destination.topic_id,
            session_id=selected.session_id,
            session_generation=selected.generation,
        )
        repeated = self.state.reject_blocked_provider_input(
            chat_id=destination.chat_id,
            message_id=705,
            topic_id=destination.topic_id,
            session_id=selected.session_id,
            session_generation=selected.generation,
        )

        self.assertIsNotNone(first)
        self.assertEqual(first, repeated)
        assert first is not None
        self.assertEqual(first.blocker_topic_id, owner_topic.topic_id)
        self.assertEqual(first.kind, "rejected")
        self.assertIn("не получил", first.telegram_html)
        self.assertEqual(self.state.provider_jobs_for_topic(destination.topic_id), ())

    def test_controller_admission_rejects_blocked_update_without_provider_job(self) -> None:
        _, owner = self.topic_session(project_id="example-project", thread_id=179, agent_id="codex")
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=180, agent_id="opencode"
        )
        self.state.set_writer_mode(owner.session_id, "local")
        message = TopicMessage(
            update_id=1900,
            message_id=1900,
            chat_id=destination.chat_id,
            thread_id=destination.thread_id,
            chat_title="Fictional group",
            sender_id=42,
            text="Fictional productive request",
        )
        admission = DurableProviderAdmission(
            state=self.state,
            telegram=cast(Any, object()),
            state_path=self.base / "state.db",
            observer_agent_id="hub",
            message_batch_quiet_ms=100,
            message_batch_max_ms=1000,
        )
        request = DurableAdmissionRequest(
            message=message,
            topic=destination,
            session=selected,
            prompt=message.text,
        )
        result = admission.admit(request)
        self.assertEqual(result, RejectedAdmission("persistent_root_blocker"))
        self.assertIsInstance(admission.admit(request), DuplicateAdmission)
        self.assertEqual(self.state.provider_jobs_for_topic(destination.topic_id), ())

    def test_concurrent_local_transfer_and_admission_have_one_winner(self) -> None:
        _, owner = self.topic_session(project_id="example-project", thread_id=181, agent_id="codex")
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=182, agent_id="opencode"
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        failures: list[Exception] = []

        def transfer() -> None:
            peer = HubState.open(self.base / "state.db")
            try:
                barrier.wait(timeout=3)
                try:
                    peer.set_writer_mode(owner.session_id, "local")
                    outcomes.append("local")
                except StateError:
                    pass
            except Exception as exc:
                failures.append(exc)
            finally:
                peer.close()

        def admit() -> None:
            peer = HubState.open(self.base / "state.db")
            try:
                barrier.wait(timeout=3)
                try:
                    peer.enqueue_provider_job(
                        idempotency_key="fictional-race:181",
                        chat_id=destination.chat_id,
                        message_id=1811,
                        topic_id=destination.topic_id,
                        agent_id=selected.agent_id,
                        session_id=selected.session_id,
                        session_generation=selected.generation,
                        model=selected.model,
                        effort=selected.effort,
                        payload_text="Fictional concurrent request",
                    )
                    outcomes.append("queued")
                except StateError:
                    pass
            except Exception as exc:
                failures.append(exc)
            finally:
                peer.close()

        threads = [threading.Thread(target=transfer), threading.Thread(target=admit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(
            (self.state.get_session(owner.session_id).writer_mode == "local"),
            (outcomes == ["local"]),
        )

    def test_legacy_queued_job_needs_exact_notice_decision_after_return(self) -> None:
        owner_topic, owner = self.topic_session(
            project_id="example-project", thread_id=171, agent_id="codex"
        )
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=172, agent_id="opencode"
        )
        waiting = self.enqueue(destination, selected, 1701)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (owner.session_id,),
            )
        self.assertEqual(self.state.materialize_held_provider_jobs(), 1)
        self.assertEqual(self.state.materialize_held_provider_jobs(), 0)
        notice = self.state.lease_root_blocker_notice("fictional-sender")
        assert notice is not None
        self.assertEqual(notice.kind, "held")
        self.assertIn("сохранён, но не начат", notice.telegram_html)
        self.state.complete_root_blocker_notice(notice, 1801)
        with self.assertRaisesRegex(StateError, "still held"):
            self.state.decide_held_provider_job(
                job_id=waiting.job_id,
                action="confirm",
                chat_id=destination.chat_id,
                thread_id=destination.thread_id,
                notice_message_id=1801,
            )
        self.state.set_writer_mode(owner.session_id, "telegram")
        later = self.enqueue(destination, selected, 1703)
        self.assertIsNone(self.state.lease_provider_job("opencode", "fictional-worker"))
        self.assertEqual(
            self.state.decide_held_provider_job(
                job_id=waiting.job_id,
                action="confirm",
                chat_id=destination.chat_id,
                thread_id=destination.thread_id,
                notice_message_id=1801,
            ),
            "confirmed",
        )
        self.assertEqual(
            self.state.decide_held_provider_job(
                job_id=waiting.job_id,
                action="confirm",
                chat_id=destination.chat_id,
                thread_id=destination.thread_id,
                notice_message_id=1801,
            ),
            "confirmed",
        )
        leased = self.state.lease_provider_job("opencode", "fictional-worker")
        assert leased is not None
        self.assertEqual(leased.job_id, waiting.job_id)
        self.assertEqual(self.state.get_provider_job(later.job_id).status, "queued")
        self.assertEqual(owner_topic.execution_scope, destination.execution_scope)

    def test_held_job_can_be_cancelled_without_releasing_local_writer(self) -> None:
        _, owner = self.topic_session(project_id="example-project", thread_id=173, agent_id="codex")
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=174, agent_id="opencode"
        )
        waiting = self.enqueue(destination, selected, 1702)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (owner.session_id,),
            )
        self.state.materialize_held_provider_jobs()
        notice = self.state.lease_root_blocker_notice("fictional-sender")
        assert notice is not None
        self.state.complete_root_blocker_notice(notice, 1802)
        with self.assertRaisesRegex(StateError, "notice or topic"):
            self.state.decide_held_provider_job(
                job_id=waiting.job_id,
                action="cancel",
                chat_id=destination.chat_id,
                thread_id=999,
                notice_message_id=1802,
            )
        self.assertEqual(
            self.state.decide_held_provider_job(
                job_id=waiting.job_id,
                action="cancel",
                chat_id=destination.chat_id,
                thread_id=destination.thread_id,
                notice_message_id=1802,
            ),
            "cancelled",
        )
        self.state.set_writer_mode(owner.session_id, "telegram")
        self.assertEqual(self.state.get_provider_job(waiting.job_id).status, "cancelled")
        self.assertIsNone(self.state.lease_provider_job("opencode", "fictional-worker"))
        released = self.state.lease_root_blocker_notice("fictional-sender")
        assert released is not None
        self.assertEqual(released.kind, "released")
        self.assertIn("отменён", released.telegram_html)

    def test_codex_return_holds_legacy_queue_in_its_own_topic(self) -> None:
        topic, session = self.topic_session(
            project_id="example-project", thread_id=175, agent_id="codex"
        )
        waiting = self.enqueue(topic, session, 1704)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (session.session_id,),
            )
        returned, created = self.state.return_codex_local_writer(
            chat_id=topic.chat_id,
            message_id=1705,
            topic_id=topic.topic_id,
            session_id=session.session_id,
            observer_agent_id="hub",
        )
        self.assertTrue(created)
        self.assertEqual(returned.writer_mode, "telegram")
        self.assertIsNone(self.state.lease_provider_job("codex", "fictional-worker"))
        hold = self.state._connection.execute(
            "SELECT hold_reason,decision FROM provider_job_holds WHERE job_id=?",
            (waiting.job_id,),
        ).fetchone()
        self.assertEqual(tuple(hold), ("local", "pending"))

    def test_schema_34_uncertainty_hold_gets_exact_owner_decision_notice(self) -> None:
        topic, session = self.topic_session(
            project_id="example-project", thread_id=176, agent_id="codex"
        )
        source = self.enqueue(topic, session, 1706)
        waiting = self.enqueue(topic, session, 1707)
        with self.state._connection:
            self.state._connection.execute(
                """INSERT INTO provider_job_holds(job_id,cause_job_id,held_at)
                   VALUES (?,?,?)""",
                (waiting.job_id, source.job_id, "2026-01-01T00:00:00+00:00"),
            )
        self.assertEqual(self.state.materialize_held_provider_jobs(), 1)
        notice = self.state.lease_root_blocker_notice("fictional-sender")
        assert notice is not None
        self.assertEqual(notice.job_id, waiting.job_id)
        self.assertIn("прежний ход", notice.telegram_html)
        self.assertEqual(self.state.materialize_held_provider_jobs(), 0)

    def test_uncertainty_resolution_notifies_rejected_and_held_topics_once(self) -> None:
        source_topic, source_session = self.topic_session(
            project_id="example-project", thread_id=177, agent_id="codex"
        )
        destination, selected = self.topic_session(
            project_id="example-project", thread_id=178, agent_id="opencode"
        )
        source = self.enqueue(source_topic, source_session, 1710)
        self.enqueue(source_topic, source_session, 1711)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='indeterminate' WHERE job_id=?",
                (source.job_id,),
            )
        self.state.reject_blocked_provider_input(
            chat_id=destination.chat_id,
            message_id=1712,
            topic_id=destination.topic_id,
            session_id=selected.session_id,
            session_generation=selected.generation,
        )
        self.assertEqual(self.state.materialize_held_provider_jobs(), 1)
        self.assertEqual(self.state.held_provider_job_count(source_topic.topic_id), 1)
        self.assertEqual(self.state.materialize_released_uncertainty_notices(), 0)
        self.state.resolve_indeterminate_job(source.job_id, "acknowledged")
        self.assertEqual(self.state.held_provider_job_count(source_topic.topic_id), 1)
        self.assertEqual(self.state.materialize_released_uncertainty_notices(), 2)
        self.assertEqual(self.state.materialize_released_uncertainty_notices(), 0)
        released = self.state._connection.execute(
            """SELECT telegram_html FROM hub_blocker_outbox
               WHERE kind='released' ORDER BY chat_id,thread_id"""
        ).fetchall()
        self.assertEqual(len(released), 2)
        self.assertTrue(any("паузе" in row[0] for row in released))
        self.assertTrue(any("не был передан" in row[0] for row in released))

    def test_reconcile_legacy_scope_keeps_local_writer_on_canonical_root(self) -> None:
        local_topic, local_session = self.topic_session(
            project_id="example-project", thread_id=741, agent_id="codex", root=self.base
        )
        self.state.set_writer_mode(local_session.session_id, "local")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope = ? WHERE topic_id = ?",
                ("project:example-project", local_topic.topic_id),
            )
        work_topic, work_session = self.topic_session(
            project_id="example-project", thread_id=742, agent_id="opencode", root=self.base
        )
        self.state.reconcile_legacy_execution_scopes({"example-project": self.base})
        with self.assertRaisesRegex(StateError, "persistent local writer"):
            self.enqueue(work_topic, work_session, 742)

        self.assertIsNone(self.state.lease_provider_job("opencode", "opencode-worker"))
        self.assertEqual(
            self.state.get_topic(local_topic.topic_id).execution_scope, f"root:{self.base}"
        )

    def test_unknown_legacy_active_scope_fails_closed_without_root_evidence(self) -> None:
        topic, session = self.topic_session(
            project_id="retired-project", thread_id=743, agent_id="codex", root=self.base
        )
        self.state.set_writer_mode(session.session_id, "local")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                ("project:retired-project", topic.topic_id),
            )

        with self.assertRaisesRegex(StateError, "ambiguous legacy execution scope"):
            self.state.reconcile_legacy_execution_scopes({"example-project": self.base})
        self.assertEqual(
            self.state.get_topic(topic.topic_id).execution_scope, "project:retired-project"
        )

    def test_unresolved_indeterminate_job_holds_the_root_until_resolution(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project",
            thread_id=75,
            agent_id="opencode",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project",
            thread_id=76,
            agent_id="antigravity",
        )
        first = self.enqueue(first_topic, first_session, 704)
        second = self.enqueue(second_topic, second_session, 705)
        leased = self.state.lease_provider_job("opencode", "opencode-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        self.state.mark_provider_job_indeterminate(
            first.job_id,
            leased.lease_token,
            error_code="fictional_unknown",
        )

        self.assertIsNone(self.state.lease_provider_job("antigravity", "agy-worker"))
        self.state.resolve_indeterminate_job(first.job_id, "acknowledged")
        released = self.state.lease_provider_job("antigravity", "agy-worker")
        self.assertIsNotNone(released)
        assert released is not None
        self.assertEqual(released.job_id, second.job_id)

    def test_different_roots_are_not_globally_serialized(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project-a",
            thread_id=77,
            agent_id="opencode",
            root=self.base / "root-a",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project-b",
            thread_id=78,
            agent_id="antigravity",
            root=self.base / "root-b",
        )
        first = self.enqueue(first_topic, first_session, 706)
        second = self.enqueue(second_topic, second_session, 707)

        first_lease = self.state.lease_provider_job(
            "opencode", "opencode-worker", max_parallel_roots=2
        )
        second_lease = self.state.lease_provider_job(
            "antigravity", "agy-worker", max_parallel_roots=2
        )

        self.assertIsNotNone(first_lease)
        self.assertIsNotNone(second_lease)
        assert first_lease is not None and second_lease is not None
        self.assertEqual(first_lease.job_id, first.job_id)
        self.assertEqual(second_lease.job_id, second.job_id)

    def test_blocked_oldest_root_does_not_starve_an_independent_root(self) -> None:
        blocked_topic, blocker_session = self.topic_session(
            project_id="example-project-a",
            thread_id=91,
            agent_id="antigravity",
            root=self.base / "root-a",
        )
        blocked_session = self.state.ensure_satellite(
            blocked_topic.topic_id, "opencode", "fictional-model", "high"
        )
        free_topic, free_session = self.topic_session(
            project_id="example-project-b",
            thread_id=92,
            agent_id="opencode",
            root=self.base / "root-b",
        )
        blocker = self.enqueue(blocked_topic, blocker_session, 718)
        blocked = self.enqueue(blocked_topic, blocked_session, 719)
        free = self.enqueue(free_topic, free_session, 720)
        blocker_lease = self.state.lease_provider_job("antigravity", "agy-worker")
        assert blocker_lease is not None and blocker_lease.lease_token is not None
        self.state.mark_provider_job_executing(blocker.job_id, blocker_lease.lease_token)

        leased = self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=2)

        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, free.job_id)
        self.assertEqual(self.state.get_provider_job(blocked.job_id).status, "queued")

    def test_same_canonical_root_outlives_a_project_id_change(self) -> None:
        root = self.base / "shared-root"
        first_topic, first_session = self.topic_session(
            project_id="old-example-project",
            thread_id=79,
            root=root,
            agent_id="opencode",
        )
        second_topic, second_session = self.topic_session(
            project_id="new-example-project",
            thread_id=80,
            root=root / ".." / root.name,
            agent_id="antigravity",
        )
        self.enqueue(first_topic, first_session, 708)
        self.enqueue(second_topic, second_session, 709)

        first_lease = self.state.lease_provider_job("opencode", "opencode-worker")
        self.assertIsNotNone(first_lease)
        self.assertIsNone(self.state.lease_provider_job("antigravity", "agy-worker"))
        self.assertEqual(first_topic.execution_scope, second_topic.execution_scope)

    def test_competing_workers_claim_only_one_job_for_a_root(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project",
            thread_id=81,
            agent_id="opencode",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project",
            thread_id=82,
            agent_id="antigravity",
        )
        self.enqueue(first_topic, first_session, 710)
        self.enqueue(second_topic, second_session, 711)
        barrier = threading.Barrier(3)
        results: list[str | None] = []

        def lease(agent_id: str, worker_id: str) -> None:
            state = HubState.open(self.base / "state.db")
            try:
                barrier.wait()
                job = state.lease_provider_job(agent_id, worker_id)
                results.append(None if job is None else job.job_id)
            finally:
                state.close()

        threads = (
            threading.Thread(target=lease, args=("opencode", "opencode-worker")),
            threading.Thread(target=lease, args=("antigravity", "agy-worker")),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(len([result for result in results if result is not None]), 1)

    def test_expired_pre_execution_lease_releases_root_and_rejects_late_start(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project",
            thread_id=83,
            agent_id="opencode",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project",
            thread_id=84,
            agent_id="antigravity",
        )
        first = self.enqueue(first_topic, first_session, 712)
        second = self.enqueue(second_topic, second_session, 713)
        clock = datetime(2026, 9, 13, tzinfo=timezone.utc)
        stale = self.state.lease_provider_job("opencode", "lost-worker", lease_seconds=1, now=clock)
        assert stale is not None and stale.lease_token is not None

        released = self.state.lease_provider_job(
            "antigravity", "agy-worker", now=clock + timedelta(seconds=2)
        )
        self.assertIsNotNone(released)
        assert released is not None
        self.assertEqual(released.job_id, second.job_id)
        with self.assertRaisesRegex(StateError, "expired"):
            self.state.mark_provider_job_executing(
                first.job_id, stale.lease_token, now=clock + timedelta(seconds=2)
            )

    def test_local_transfer_and_worker_claim_are_atomic(self) -> None:
        local_topic, local_session = self.topic_session(
            project_id="example-project",
            thread_id=85,
            agent_id="codex",
        )
        work_topic, work_session = self.topic_session(
            project_id="example-project",
            thread_id=86,
            agent_id="opencode",
        )
        self.enqueue(work_topic, work_session, 714)
        barrier = threading.Barrier(3)
        results: list[str] = []

        def claim_local() -> None:
            state = HubState.open(self.base / "state.db")
            try:
                barrier.wait()
                try:
                    state.set_writer_mode(local_session.session_id, "local")
                    results.append("local")
                except StateError:
                    results.append("local-blocked")
            finally:
                state.close()

        def claim_job() -> None:
            state = HubState.open(self.base / "state.db")
            try:
                barrier.wait()
                job = state.lease_provider_job("opencode", "opencode-worker")
                results.append("job" if job is not None else "job-blocked")
            finally:
                state.close()

        threads = (threading.Thread(target=claim_local), threading.Thread(target=claim_job))
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertIn(sorted(results), (["job", "local-blocked"], ["job-blocked", "local"]))

    def test_real_workers_never_enter_two_providers_for_one_root(self) -> None:
        harness = FaultMatrixHarness(self.base)
        root = harness.registry.require_project("example-project").root
        first_topic, first_session = self.topic_session(
            project_id="example-project",
            thread_id=87,
            agent_id="opencode",
            root=root,
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project",
            thread_id=88,
            agent_id="antigravity",
            root=root,
        )
        self.enqueue(first_topic, first_session, 715)
        self.enqueue(second_topic, second_session, 716)

        provider_entered = threading.Event()

        class BlockingAdapter:
            def __init__(self, runtime: str) -> None:
                self.runtime = runtime
                self.entered = threading.Event()
                self.release = threading.Event()

            def run_turn(self, **_kwargs: object) -> ExternalTurnResult:
                self.entered.set()
                provider_entered.set()
                if not self.release.wait(5):
                    raise RuntimeError("fictional provider release timed out")
                return ExternalTurnResult(
                    self.runtime,
                    f"{self.runtime} completed",
                    f"{self.runtime}-session",
                    "fictional-model",
                )

        first_adapter = BlockingAdapter("opencode")
        second_adapter = BlockingAdapter("antigravity")
        done = threading.Event()
        failures: list[BaseException] = []

        def run(agent_id: str, adapter: BlockingAdapter) -> None:
            worker = harness.worker(agent_id, adapter)
            try:
                worker.run_cycle()
            except BaseException as exc:
                failures.append(exc)
            finally:
                worker.close()
                done.set()

        threads = (
            threading.Thread(target=run, args=("opencode", first_adapter)),
            threading.Thread(target=run, args=("antigravity", second_adapter)),
        )
        entered_in_time = False
        loser_finished = False
        entered_count = 0
        try:
            for thread in threads:
                thread.start()
            entered_in_time = provider_entered.wait(3)
            deadline = time.monotonic() + 3
            while (
                not done.is_set()
                and not (first_adapter.entered.is_set() and second_adapter.entered.is_set())
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            loser_finished = done.is_set()
            entered_count = int(first_adapter.entered.is_set()) + int(
                second_adapter.entered.is_set()
            )
        finally:
            first_adapter.release.set()
            second_adapter.release.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertTrue(entered_in_time, "one worker must enter its provider")
        self.assertTrue(loser_finished, "one blocked-root worker must return without a lease")
        self.assertEqual(entered_count, 1)
        self.assertEqual(failures, [])

    def test_real_worker_waits_for_local_writer_return(self) -> None:
        harness = FaultMatrixHarness(self.base)
        root = harness.registry.require_project("example-project").root
        local_topic, local_session = self.topic_session(
            project_id="example-project",
            thread_id=89,
            agent_id="codex",
            root=root,
        )
        work_topic, work_session = self.topic_session(
            project_id="example-project",
            thread_id=90,
            agent_id="opencode",
            root=root,
        )
        waiting = self.enqueue(work_topic, work_session, 717)
        # Legacy state could contain accepted work before the local lease was
        # recorded; the new release must pause it before returning ownership.
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (local_session.session_id,),
            )
        adapter = RecordingAdapter("opencode")
        worker = harness.worker("opencode", adapter)
        try:
            self.assertFalse(worker.run_cycle())
            self.assertEqual(adapter.calls, [])
            self.state.set_writer_mode(local_session.session_id, "telegram")
            self.assertFalse(worker.run_cycle())
            self.assertEqual(adapter.calls, [])
            hold = self.state._connection.execute(
                "SELECT hold_reason,decision FROM provider_job_holds WHERE job_id=?",
                (waiting.job_id,),
            ).fetchone()
            self.assertEqual(tuple(hold), ("local", "pending"))
            self.assertEqual(local_topic.execution_scope, work_topic.execution_scope)
        finally:
            worker.close()

    def test_worker_reconciles_a_retained_legacy_local_writer_before_leasing(self) -> None:
        harness = FaultMatrixHarness(self.base)
        state = HubState.open(harness.config.state_path)
        root = harness.registry.require_project("example-project").root
        try:
            old = state.observe_topic(
                project_id="example-project",
                chat_id=harness.chat_id,
                thread_id=91,
                title="Fictional old",
                execution_root=root,
            )
            local = state.activate_agent(old.topic_id, "codex", "fictional", "high")
            state.set_writer_mode(local.session_id, "local")
            with state._connection:
                state._connection.execute(
                    "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                    ("project:example-project", old.topic_id),
                )
            new = state.observe_topic(
                project_id="example-project",
                chat_id=harness.chat_id,
                thread_id=92,
                title="Fictional new",
                execution_root=root,
            )
            session = state.activate_agent(new.topic_id, "opencode", "fictional", "high")
            state.enqueue_provider_job(
                idempotency_key="legacy:719",
                chat_id=harness.chat_id,
                message_id=719,
                topic_id=new.topic_id,
                agent_id=session.agent_id,
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="fictional legacy scope task",
            )
            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertFalse(worker.run_cycle())
                self.assertEqual(adapter.calls, [])
                self.assertEqual(state.get_topic(old.topic_id).execution_scope, f"root:{root}")
            finally:
                worker.close()
        finally:
            state.close()

    def test_controller_observation_upgrades_topic_to_canonical_root_scope(self) -> None:
        harness = FaultMatrixHarness(self.base)
        controller = harness.controller()
        try:
            self.assertTrue(controller.handle_update(harness.update(721, 93, "/menu")))
            topic = controller.state.find_topic(harness.chat_id, 93)
            self.assertIsNotNone(topic)
            assert topic is not None
            self.assertEqual(
                topic.execution_scope,
                f"root:{harness.registry.require_project('example-project').root}",
            )
        finally:
            controller.state.close()


if __name__ == "__main__":
    unittest.main()
