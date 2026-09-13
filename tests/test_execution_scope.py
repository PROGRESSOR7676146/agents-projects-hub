from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.state import HubState, StateError
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
        waiting = self.enqueue(work_topic, work_session, 703)

        self.assertIsNone(self.state.lease_provider_job("opencode", "opencode-worker"))
        self.state.set_writer_mode(local_session.session_id, "telegram")
        leased = self.state.lease_provider_job("opencode", "opencode-worker")
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, waiting.job_id)
        self.assertEqual(local_topic.execution_scope, work_topic.execution_scope)

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

        first_lease = self.state.lease_provider_job("opencode", "opencode-worker")
        second_lease = self.state.lease_provider_job("antigravity", "agy-worker")

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

        leased = self.state.lease_provider_job("opencode", "opencode-worker")

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
        self.state.set_writer_mode(local_session.session_id, "local")
        self.enqueue(work_topic, work_session, 717)
        adapter = RecordingAdapter("opencode")
        worker = harness.worker("opencode", adapter)
        try:
            self.assertFalse(worker.run_cycle())
            self.assertEqual(adapter.calls, [])
            self.state.set_writer_mode(local_session.session_id, "telegram")
            self.assertTrue(worker.run_cycle())
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(local_topic.execution_scope, work_topic.execution_scope)
        finally:
            worker.close()

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
