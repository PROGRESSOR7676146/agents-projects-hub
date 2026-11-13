from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.codex_live_control import CodexLiveControl
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.progress_delivery import ProgressDeliveryQueue
from hermes_codex_router.session_connect import SessionConnectStore
from hermes_codex_router.state import HubState
from tests import test_codex_worker as codex_fixtures
from tests import test_external_worker as worker_fixtures
from tests import test_outbox_sender as sender_fixtures


class IdlePollingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile="full")
        self.addCleanup(self.state.close)

    def test_empty_polls_do_not_wait_for_an_unrelated_writer(self) -> None:
        # A fresh declaration must still be kept alive for capacity fairness.
        self.assertIsNone(
            self.state.lease_provider_job("codex", "codex-worker", scheduler_agents=("codex",))
        )
        contender = HubState.open_existing(
            self.path, codex_permission_profile="full", contention_timeout_seconds=0.01
        )
        self.addCleanup(contender.close)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        connect = SessionConnectStore(contender)
        progress = ProgressDeliveryQueue(contender)
        control = CodexLiveControl(
            config=cast(Any, SimpleNamespace(codex_permission_profile="full")),
            state_factory=lambda: contender,
            client_factory=cast(Any, lambda: None),
            job=cast(Any, SimpleNamespace(job_id="fictional-parent")),
            worker_id="worker",
            thread_id="fictional-thread",
            turn_id="fictional-turn",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
        )
        blocker.execute("BEGIN IMMEDIATE")
        try:
            checks = (
                ("stale jobs", lambda: contender.recover_stale_provider_jobs(agent_id="codex")),
                (
                    "empty jobs",
                    lambda: contender.lease_provider_job(
                        "codex", "codex-worker", scheduler_agents=("codex",)
                    ),
                ),
                ("connect requests", lambda: connect.lease_worker("codex-worker")),
                ("stale connect deliveries", connect.recover_stale_outbox),
                ("connect deliveries", lambda: connect.lease_outbox("sender")),
                ("final delivery", lambda: contender.lease_telegram_outbox("codex", "sender")),
                (
                    "stale final deliveries",
                    lambda: contender.recover_stale_telegram_outbox(sender_agent_ids=("codex",)),
                ),
                ("progress delivery", lambda: progress.lease("codex", "sender")),
                ("stale progress", lambda: progress.recover_stale(("codex",))),
                ("terminal progress", lambda: progress.supersede_terminal(("codex",))),
                (
                    "ineligible steering",
                    lambda: control._steer(contender),
                ),
            )
            for name, call in checks:
                with self.subTest(check=name):
                    call()
        finally:
            blocker.execute("ROLLBACK")

    def test_real_final_delivery_claim_stays_exclusive(self) -> None:
        self.assertIsNone(self.state.lease_telegram_outbox("codex", "sender"))
        topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "fictional-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="fictional:1",
            chat_id=topic.chat_id,
            message_id=1,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="fictional work",
        )
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        self.state.commit_provider_result(
            job.job_id,
            leased.lease_token,
            visible_response="result",
            sender_agent_id="codex",
            telegram_html="result",
        )
        expected = self.state.get_telegram_outbox_for_job(job.job_id)
        barrier = threading.Barrier(2)
        results: list[str | None] = []
        failures: list[BaseException] = []

        def claim(name: str) -> None:
            state = HubState.open_existing(self.path, codex_permission_profile="full")
            try:
                barrier.wait(timeout=5)
                result = state.lease_telegram_outbox("codex", name)
                results.append(None if result is None else result.outbox_id)
            except BaseException as error:
                failures.append(error)
            finally:
                state.close()

        threads = [threading.Thread(target=claim, args=(f"sender-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(results.count(expected.outbox_id), 1)
        self.assertEqual(results.count(None), 1)
        self.assertEqual(self.state.get_telegram_outbox(expected.outbox_id).attempt_count, 1)

    def test_idle_scheduler_declaration_remains_fresh_and_capacity_changes_are_immediate(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)

        def poll(capacity: int, at: datetime) -> None:
            self.assertIsNone(
                self.state.lease_provider_job(
                    "codex",
                    "codex-worker",
                    scheduler_agents=("codex",),
                    max_parallel_roots=capacity,
                    now=at,
                )
            )

        poll(3, now)
        poll(3, now + timedelta(seconds=5))
        before = self.state._connection.execute(
            "SELECT declared_capacity, observed_at FROM execution_scheduler_workers WHERE agent_id=?",
            ("codex-worker",),
        ).fetchone()
        self.assertEqual(tuple(before), (3, now.isoformat()))
        poll(1, now + timedelta(seconds=6))
        reduced = self.state._connection.execute(
            "SELECT declared_capacity, observed_at FROM execution_scheduler_workers WHERE agent_id=?",
            ("codex-worker",),
        ).fetchone()
        self.assertEqual(tuple(reduced), (1, (now + timedelta(seconds=6)).isoformat()))
        poll(1, now + timedelta(seconds=21))
        refreshed = self.state._connection.execute(
            "SELECT declared_capacity, observed_at FROM execution_scheduler_workers WHERE agent_id=?",
            ("codex-worker",),
        ).fetchone()
        self.assertEqual(tuple(refreshed), (1, (now + timedelta(seconds=21)).isoformat()))

    def test_idle_sender_cycle_has_bounded_writer_transactions(self) -> None:
        fixture = sender_fixtures.TelegramOutboxSenderTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        template = fixture.config.agents[0]
        agents = (
            replace(template, agent_id="codex", runtime="codex"),
            replace(template, agent_id="claude", runtime="claude"),
            *fixture.config.agents,
        )
        config = replace(
            fixture.config,
            agents=agents,
            hub_bot=HubTelegramBot("example_hub_bot", Path("/example/unused")),
        )
        bots = {
            key: sender_fixtures.Bot()
            for key in ("hub", "codex", "claude", "opencode", "antigravity")
        }
        sender = TelegramOutboxSender(config, telegram_bots=bots)
        self.addCleanup(sender.close)
        transactions: list[str] = []
        sender.state._connection.set_trace_callback(
            lambda sql: (
                transactions.append(sql) if sql.upper().startswith("BEGIN IMMEDIATE") else None
            )
        )
        try:
            self.assertFalse(sender.run_cycle())
        finally:
            sender.state._connection.set_trace_callback(None)
        self.assertLessEqual(len(transactions), 12)
        self.assertTrue(all(not bot.sent and not bot.documents for bot in bots.values()))

    def test_idle_worker_health_is_bounded_but_changes_are_published(self) -> None:
        fixture = worker_fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        worker = ExternalQueueWorker(
            fixture.config,
            "opencode",
            registry=fixture.registry,
            adapter=cast(Any, worker_fixtures.Adapter("opencode")),
        )
        self.addCleanup(worker.close)
        initial = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert initial is not None
        worker._publish_health()
        unchanged = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert unchanged is not None
        self.assertEqual(unchanged.heartbeat_at, initial.heartbeat_at)
        worker._last_error_code = "fictional_error"
        worker._publish_health()
        changed = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert changed is not None
        self.assertEqual(changed.error_code, "fictional_error")
        worker._last_idle_health_publish -= 11
        worker._publish_health()
        refreshed = worker.state.get_runtime_health("provider_worker", worker.worker_id)
        assert refreshed is not None
        self.assertGreater(refreshed.heartbeat_at, changed.heartbeat_at)

    def test_idle_health_cache_cannot_hide_a_later_busy_publication(self) -> None:
        fixture = worker_fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        worker = ExternalQueueWorker(
            fixture.config,
            "opencode",
            registry=fixture.registry,
            adapter=cast(Any, worker_fixtures.Adapter("opencode")),
        )
        self.addCleanup(worker.close)
        worker._last_idle_health_signature = None
        idle_written = threading.Event()
        release_idle = threading.Event()
        idle_state = Mock(spec=HubState)
        busy_state = Mock(spec=HubState)
        verify_state = Mock(spec=HubState)

        def delayed_idle_write(**_kwargs: Any) -> None:
            # The idle DB write has finished, but its caller has not yet
            # updated the local cache when a late busy heartbeat publishes.
            idle_written.set()
            release_idle.wait(timeout=10)

        idle_state.upsert_runtime_health.side_effect = delayed_idle_write
        idle_thread = threading.Thread(target=lambda: worker._publish_health(state=idle_state))
        idle_thread.start()
        try:
            self.assertTrue(idle_written.wait(timeout=5))
            worker._publish_health(
                state=busy_state,
                activity_state="executing",
                active_job=cast(Any, SimpleNamespace(job_id="fictional", lease_expires_at=None)),
            )
            busy_state.upsert_runtime_health.assert_called_once()
        finally:
            release_idle.set()
            idle_thread.join(timeout=5)
        self.assertFalse(idle_thread.is_alive())
        worker._publish_health(state=verify_state)
        verify_state.upsert_runtime_health.assert_called_once()

    def test_late_busy_health_cannot_make_an_overlapping_idle_cache_fresh(self) -> None:
        fixture = worker_fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        worker = ExternalQueueWorker(
            fixture.config,
            "opencode",
            registry=fixture.registry,
            adapter=cast(Any, worker_fixtures.Adapter("opencode")),
        )
        self.addCleanup(worker.close)
        worker._last_idle_health_signature = None
        busy_started = threading.Event()
        release_busy = threading.Event()
        idle_written = threading.Event()
        release_idle = threading.Event()
        busy_state = Mock(spec=HubState)
        idle_state = Mock(spec=HubState)
        verify_state = Mock(spec=HubState)

        def delayed_busy_write(**_kwargs: Any) -> None:
            busy_started.set()
            release_busy.wait(timeout=10)

        def paused_idle_write(**_kwargs: Any) -> None:
            # The idle write commits before the old busy write, but its
            # publication does not finish until after the busy publication.
            idle_written.set()
            release_idle.wait(timeout=10)

        busy_state.upsert_runtime_health.side_effect = delayed_busy_write
        idle_state.upsert_runtime_health.side_effect = paused_idle_write
        busy_thread = threading.Thread(
            target=lambda: worker._publish_health(
                state=busy_state,
                activity_state="executing",
                active_job=cast(Any, SimpleNamespace(job_id="fictional", lease_expires_at=None)),
            )
        )
        idle_thread = threading.Thread(target=lambda: worker._publish_health(state=idle_state))
        busy_thread.start()
        try:
            self.assertTrue(busy_started.wait(timeout=5))
            idle_thread.start()
            self.assertTrue(idle_written.wait(timeout=5))
            release_busy.set()
            busy_thread.join(timeout=5)
            self.assertFalse(busy_thread.is_alive())
        finally:
            release_busy.set()
            release_idle.set()
            busy_thread.join(timeout=5)
            if idle_thread.ident is not None:
                idle_thread.join(timeout=5)
        self.assertFalse(idle_thread.is_alive())
        worker._publish_health(state=verify_state)
        verify_state.upsert_runtime_health.assert_called_once()

    def test_full_profile_still_observes_owner_stop(self) -> None:
        state = Mock(spec=HubState)
        state.pending_emergency_stop_for_job.return_value = "fictional-stop"
        control = CodexLiveControl(
            config=cast(Any, SimpleNamespace(codex_permission_profile="full")),
            state_factory=lambda: state,
            client_factory=cast(Any, lambda: None),
            job=cast(Any, SimpleNamespace(job_id="fictional-parent")),
            worker_id="worker",
            thread_id="fictional-thread",
            turn_id="fictional-turn",
            transport_mode="socket",
            close_owned_turn_client=lambda: None,
            poll_seconds=0.001,
        )
        with patch.object(control, "_interrupt", return_value=True) as interrupt:
            control.start()
            assert control._thread is not None
            control._thread.join(timeout=5)
            control.stop_and_join()
            interrupt.assert_called_once_with(state, "fictional-stop")
        state.lease_steer_followup.assert_not_called()

    def test_fresh_connect_thread_suppresses_only_the_duplicate_foreground_poll(self) -> None:
        fixture = codex_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        supervisor = codex_fixtures.WorkerSupervisor(codex_fixtures.WorkerClient())
        worker = ExternalQueueWorker(
            fixture.config, "codex", registry=fixture.registry, supervisor=cast(Any, supervisor)
        )
        self.addCleanup(worker.close)
        background = Mock(spec=threading.Thread)
        background.is_alive.return_value = True
        worker._connect_thread = background
        worker._last_connect_poll = time.monotonic()
        with patch.object(worker, "_run_connect_cycle", return_value=True) as foreground:
            self.assertFalse(worker.run_cycle())
            foreground.assert_not_called()
            worker._last_connect_poll = None
            self.assertTrue(worker.run_cycle())
            foreground.assert_called_once()
            foreground.reset_mock()
            worker._last_connect_poll = time.monotonic()
            worker._last_connect_poll -= 2
            self.assertTrue(worker.run_cycle())
            foreground.assert_called_once()
            foreground.reset_mock()
            background.is_alive.return_value = False
            self.assertTrue(worker.run_cycle())
            foreground.assert_called_once()

    def test_stalled_connect_thread_allows_foreground_to_serve_other_work(self) -> None:
        fixture = codex_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        supervisor = codex_fixtures.WorkerSupervisor(codex_fixtures.WorkerClient())
        worker = ExternalQueueWorker(
            fixture.config, "codex", registry=fixture.registry, supervisor=cast(Any, supervisor)
        )
        self.addCleanup(worker.close)
        blocked = threading.Event()
        release = threading.Event()
        background_calls = 0
        foreground_calls = 0

        def poll(*, state: HubState | None = None) -> bool:
            nonlocal background_calls, foreground_calls
            if state is None:
                foreground_calls += 1
                return True
            background_calls += 1
            if background_calls > 1:
                blocked.set()
                release.wait(timeout=10)
            return False

        with patch.object(worker, "_run_connect_cycle", side_effect=poll):
            background = threading.Thread(target=worker._run_connect_forever)
            worker._connect_thread = background
            background.start()
            try:
                self.assertTrue(blocked.wait(timeout=5))
                self.assertIsNotNone(worker._last_connect_poll)
                worker._last_connect_poll = time.monotonic()
                self.assertFalse(worker.run_cycle())
                self.assertEqual(foreground_calls, 0)
                worker._last_connect_poll -= 2
                self.assertTrue(worker.run_cycle())
                self.assertEqual(foreground_calls, 1)
                self.assertTrue(background.is_alive())
            finally:
                worker._stop.set()
                release.set()
                background.join(timeout=5)
            self.assertFalse(background.is_alive())


if __name__ == "__main__":
    unittest.main()
