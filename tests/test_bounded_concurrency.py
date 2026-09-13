from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.runtime_health import project_runtime_health
from hermes_codex_router.state import HubState
from hermes_codex_router.worktrees import create_worktree
from tests.fault_matrix_support import FaultMatrixHarness


class CapturingAdapter:
    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.cwds: list[Path] = []

    def run_turn(self, **kwargs: object) -> ExternalTurnResult:
        self.cwds.append(Path(str(kwargs["cwd"])))
        return ExternalTurnResult(
            self.runtime,
            f"{self.runtime} completed",
            f"{self.runtime}-session",
            "fictional-model",
        )


class BoundedConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.harness = FaultMatrixHarness(self.base)
        self.state = HubState.open(self.harness.config.state_path)
        self.root = self.harness.registry.require_project("example-project").root

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def topic_session(
        self,
        *,
        project_id: str,
        thread_id: int,
        agent_id: str,
        root: Path,
    ):
        topic = self.state.observe_topic(
            project_id=project_id,
            chat_id=-1001234567890,
            thread_id=thread_id,
            title=f"Fictional topic {thread_id}",
            execution_root=root,
        )
        session = self.state.activate_agent(topic.topic_id, agent_id, "fictional-model", "high")
        return topic, session

    def enqueue(self, topic, session, message_id: int):
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"bounded:{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text=f"fictional bounded request {message_id}",
        )
        return job

    def publish_worker(self, agent_id: str, clock: datetime) -> None:
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id=f"{agent_id}-worker",
            runtime=agent_id,
            agent_id=agent_id,
            pid=1234,
            process_start_marker=f"{agent_id}-start",
            started_at=clock,
            heartbeat_at=clock,
        )

    def test_config_defaults_to_one_parallel_root(self) -> None:
        self.assertEqual(self.harness.config.max_parallel_roots, 1)

    def test_capacity_one_blocks_a_different_root_and_two_allows_it(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project-a",
            thread_id=101,
            agent_id="opencode",
            root=self.base / "root-a",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project-b",
            thread_id=102,
            agent_id="antigravity",
            root=self.base / "root-b",
        )
        first = self.enqueue(first_topic, first_session, 801)
        second = self.enqueue(second_topic, second_session, 802)
        first_lease = self.state.lease_provider_job(
            "opencode", "opencode-worker", max_parallel_roots=1
        )
        assert first_lease is not None and first_lease.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, first_lease.lease_token)

        self.assertIsNone(
            self.state.lease_provider_job("antigravity", "antigravity-worker", max_parallel_roots=1)
        )
        allowed = self.state.lease_provider_job(
            "antigravity", "antigravity-worker", max_parallel_roots=2
        )
        self.assertIsNotNone(allowed)
        assert allowed is not None
        self.assertEqual(allowed.job_id, second.job_id)

    def test_capacity_reduction_drains_without_cancelling_active_jobs(self) -> None:
        jobs = []
        for index, agent_id in enumerate(("opencode", "antigravity", "opencode"), 1):
            topic, session = self.topic_session(
                project_id=f"example-project-{index}",
                thread_id=102 + index,
                agent_id=agent_id,
                root=self.base / f"root-{index}",
            )
            jobs.append(self.enqueue(topic, session, 802 + index))
        first = self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=2)
        second = self.state.lease_provider_job(
            "antigravity", "antigravity-worker", max_parallel_roots=2
        )
        assert first is not None and first.lease_token is not None
        assert second is not None and second.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, first.lease_token)
        self.state.mark_provider_job_executing(second.job_id, second.lease_token)

        self.assertIsNone(
            self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=1)
        )
        self.state.fail_provider_job(
            first.job_id,
            first.lease_token,
            error_class="fictional",
            error_code="fictional_done",
        )
        self.assertIsNone(
            self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=1)
        )
        self.state.fail_provider_job(
            second.job_id,
            second.lease_token,
            error_class="fictional",
            error_code="fictional_done",
        )
        drained = self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=1)
        self.assertIsNotNone(drained)
        assert drained is not None
        self.assertEqual(drained.job_id, jobs[2].job_id)

    def test_live_workers_use_the_lowest_advertised_capacity_during_reconfiguration(self) -> None:
        clock = datetime.now(timezone.utc)
        agents = ("opencode", "antigravity")
        for index, agent_id in enumerate(agents, 1):
            topic, session = self.topic_session(
                project_id=f"example-project-{index}",
                thread_id=1050 + index,
                agent_id=agent_id,
                root=self.base / f"mixed-root-{index}",
            )
            self.enqueue(topic, session, 850 + index)
            self.publish_worker(agent_id, clock)

        first = self.state.lease_provider_job(
            "opencode",
            "opencode-worker",
            max_parallel_roots=1,
            scheduler_agents=agents,
            now=clock,
        )
        assert first is not None and first.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, first.lease_token, now=clock)

        self.assertIsNone(
            self.state.lease_provider_job(
                "antigravity",
                "antigravity-worker",
                max_parallel_roots=2,
                scheduler_agents=agents,
                now=clock,
            )
        )

    def test_expired_slot_becomes_uncertain_without_blocking_an_independent_root(self) -> None:
        clock = datetime.now(timezone.utc)
        first_topic, first_session = self.topic_session(
            project_id="example-project-a",
            thread_id=113,
            agent_id="opencode",
            root=self.base / "root-a",
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project-b",
            thread_id=114,
            agent_id="antigravity",
            root=self.base / "root-b",
        )
        first = self.enqueue(first_topic, first_session, 813)
        second = self.enqueue(second_topic, second_session, 814)
        lease = self.state.lease_provider_job(
            "opencode",
            "opencode-worker",
            lease_seconds=1,
            max_parallel_roots=1,
            now=clock,
        )
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, lease.lease_token, now=clock)
        later = clock + timedelta(seconds=2)

        recovery = self.state.recover_stale_provider_jobs(now=later)
        self.assertEqual(recovery.indeterminate_job_ids, (first.job_id,))
        peer = self.state.lease_provider_job(
            "antigravity",
            "antigravity-worker",
            max_parallel_roots=1,
            now=later,
        )

        self.assertIsNotNone(peer)
        assert peer is not None
        self.assertEqual(peer.job_id, second.job_id)
        self.assertEqual(
            self.state.execution_capacity_snapshot(1, now=later)["blocked_uncertain_scopes"],
            1,
        )

    def test_targeted_stop_releases_only_its_slot(self) -> None:
        active = []
        for index, agent_id in enumerate(("opencode", "antigravity"), 1):
            topic, session = self.topic_session(
                project_id=f"example-project-{index}",
                thread_id=114 + index,
                agent_id=agent_id,
                root=self.base / f"root-{index}",
            )
            self.enqueue(topic, session, 814 + index)
            leased = self.state.lease_provider_job(
                agent_id, f"{agent_id}-worker", max_parallel_roots=2
            )
            assert leased is not None and leased.lease_token is not None
            active.append(self.state.mark_provider_job_executing(leased.job_id, leased.lease_token))

        first = active[0]
        assert first.lease_token is not None
        self.state.cancel_active_provider_job(first.job_id, first.lease_token)

        self.assertEqual(self.state.get_provider_job(active[0].job_id).status, "cancelled")
        self.assertEqual(self.state.get_provider_job(active[1].job_id).status, "executing")
        self.assertEqual(self.state.execution_capacity_snapshot(2)["occupied"], 1)

    def test_committed_result_releases_root_but_retains_same_topic_fifo(self) -> None:
        first_topic, first_session = self.topic_session(
            project_id="example-project",
            thread_id=123,
            agent_id="opencode",
            root=self.root,
        )
        peer_topic, peer_session = self.topic_session(
            project_id="example-project-alias",
            thread_id=124,
            agent_id="antigravity",
            root=self.root,
        )
        first = self.enqueue(first_topic, first_session, 823)
        successor = self.enqueue(first_topic, first_session, 824)
        peer = self.enqueue(peer_topic, peer_session, 825)
        lease = self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=1)
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, lease.lease_token)
        self.state.commit_provider_result(
            first.job_id,
            lease.lease_token,
            visible_response="fictional result",
            sender_agent_id="opencode",
            telegram_html="fictional result",
        )

        self.assertIsNone(
            self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=1)
        )
        peer_lease = self.state.lease_provider_job(
            "antigravity", "antigravity-worker", max_parallel_roots=1
        )

        self.assertIsNotNone(peer_lease)
        assert peer_lease is not None
        self.assertEqual(peer_lease.job_id, peer.job_id)
        self.assertEqual(self.state.get_provider_job(successor.job_id).status, "queued")

    def test_competing_workers_cannot_overfill_the_last_slot(self) -> None:
        for index, agent_id in enumerate(("codex", "opencode", "antigravity"), 1):
            topic, session = self.topic_session(
                project_id=f"example-project-{index}",
                thread_id=118 + index,
                agent_id=agent_id,
                root=self.base / f"capacity-root-{index}",
            )
            self.enqueue(topic, session, 818 + index)
        active = self.state.lease_provider_job("codex", "codex-worker", max_parallel_roots=2)
        assert active is not None and active.lease_token is not None
        self.state.mark_provider_job_executing(active.job_id, active.lease_token)
        barrier = threading.Barrier(3)
        results: list[str] = []

        def compete(agent_id: str) -> None:
            contender = HubState.open(self.harness.config.state_path)
            try:
                barrier.wait()
                leased = contender.lease_provider_job(
                    agent_id, f"{agent_id}-worker", max_parallel_roots=2
                )
                if leased is not None:
                    results.append(leased.job_id)
            finally:
                contender.close()

        threads = [
            threading.Thread(target=compete, args=(agent_id,))
            for agent_id in ("opencode", "antigravity")
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(self.state.execution_capacity_snapshot(2)["occupied"], 2)

    def test_round_robin_fairness_prefers_the_live_agent_waiting_longest(self) -> None:
        clock = datetime.now(timezone.utc)
        older_topic, older_session = self.topic_session(
            project_id="example-project-a",
            thread_id=106,
            agent_id="antigravity",
            root=self.base / "root-a",
        )
        newer_topic, newer_session = self.topic_session(
            project_id="example-project-b",
            thread_id=107,
            agent_id="opencode",
            root=self.base / "root-b",
        )
        older = self.enqueue(older_topic, older_session, 806)
        newer = self.enqueue(newer_topic, newer_session, 807)
        self.publish_worker("opencode", clock)
        self.publish_worker("antigravity", clock)
        agents = ("opencode", "antigravity")

        self.assertIsNone(
            self.state.lease_provider_job(
                "opencode",
                "opencode-worker",
                max_parallel_roots=1,
                scheduler_agents=agents,
                now=clock,
            )
        )
        first = self.state.lease_provider_job(
            "antigravity",
            "antigravity-worker",
            max_parallel_roots=1,
            scheduler_agents=agents,
            now=clock,
        )
        assert first is not None and first.lease_token is not None
        self.assertEqual(first.job_id, older.job_id)
        self.state.fail_provider_job(
            first.job_id,
            first.lease_token,
            error_class="fictional",
            error_code="fictional_done",
        )
        second = self.state.lease_provider_job(
            "opencode",
            "opencode-worker",
            max_parallel_roots=1,
            scheduler_agents=agents,
            now=clock,
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.job_id, newer.job_id)

    def test_stale_worker_does_not_hold_the_fairness_turn(self) -> None:
        clock = datetime.now(timezone.utc)
        stale_topic, stale_session = self.topic_session(
            project_id="example-project-a",
            thread_id=108,
            agent_id="antigravity",
            root=self.base / "root-a",
        )
        ready_topic, ready_session = self.topic_session(
            project_id="example-project-b",
            thread_id=109,
            agent_id="opencode",
            root=self.base / "root-b",
        )
        self.enqueue(stale_topic, stale_session, 808)
        ready = self.enqueue(ready_topic, ready_session, 809)
        self.publish_worker("antigravity", clock - timedelta(minutes=5))
        self.publish_worker("opencode", clock)

        leased = self.state.lease_provider_job(
            "opencode",
            "opencode-worker",
            max_parallel_roots=1,
            scheduler_agents=("opencode", "antigravity"),
            now=clock,
        )

        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, ready.job_id)

    def test_passive_capacity_snapshot_has_bounded_owner_identity(self) -> None:
        topic, session = self.topic_session(
            project_id="example-project",
            thread_id=110,
            agent_id="opencode",
            root=self.root,
        )
        job = self.enqueue(topic, session, 810)
        lease = self.state.lease_provider_job("opencode", "opencode-worker", max_parallel_roots=2)
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)

        snapshot = self.state.execution_capacity_snapshot(2)

        self.assertEqual(
            (snapshot["capacity"], snapshot["occupied"], snapshot["available"]), (2, 1, 1)
        )
        self.assertEqual(
            snapshot["owners"],
            [
                {
                    "worker_instance": "opencode-worker",
                    "agent_id": "opencode",
                    "phase": "executing",
                }
            ],
        )
        self.assertNotIn(str(self.root), str(snapshot))
        projected = project_runtime_health(
            self.state, replace(self.harness.config, max_parallel_roots=2)
        )
        self.assertEqual(projected["execution_capacity"], snapshot)

    def test_lane_binding_changes_execution_scope_and_real_worker_cwd(self) -> None:
        lane_root, branch = create_worktree(self.harness.registry.projects[0], "parallel")
        topic, session = self.topic_session(
            project_id="example-project",
            thread_id=111,
            agent_id="opencode",
            root=self.root,
        )
        self.state.register_lane(
            lane_id="parallel",
            project_id="example-project",
            worktree_path=lane_root,
            branch_name=branch,
        )
        self.state.bind_lane("parallel", topic.topic_id)

        observed = self.state.observe_topic(
            project_id="example-project",
            chat_id=topic.chat_id,
            thread_id=topic.thread_id,
            title=topic.title,
            execution_root=self.root,
        )
        self.assertEqual(observed.execution_scope, f"root:{lane_root}")
        self.enqueue(observed, session, 811)
        adapter = CapturingAdapter("opencode")
        worker = self.harness.worker("opencode", adapter)
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(adapter.cwds, [lane_root])

    def test_archiving_idle_lane_returns_topic_to_project_scope(self) -> None:
        lane_root, branch = create_worktree(self.harness.registry.projects[0], "temporary")
        topic, _ = self.topic_session(
            project_id="example-project",
            thread_id=112,
            agent_id="opencode",
            root=self.root,
        )
        self.state.register_lane(
            lane_id="temporary",
            project_id="example-project",
            worktree_path=lane_root,
            branch_name=branch,
        )
        self.state.bind_lane("temporary", topic.topic_id)

        self.state.archive_lane("temporary")

        archived = self.state.get_topic(topic.topic_id)
        self.assertEqual(archived.execution_scope, "project:example-project")

    def test_lane_scope_changes_refuse_pending_provider_work(self) -> None:
        lane_root, branch = create_worktree(self.harness.registry.projects[0], "busy")
        topic, session = self.topic_session(
            project_id="example-project",
            thread_id=117,
            agent_id="opencode",
            root=self.root,
        )
        self.state.register_lane(
            lane_id="busy",
            project_id="example-project",
            worktree_path=lane_root,
            branch_name=branch,
        )
        self.enqueue(topic, session, 817)
        with self.assertRaisesRegex(RuntimeError, "active or unresolved"):
            self.state.bind_lane("busy", topic.topic_id)
        self.assertEqual(self.state.get_topic(topic.topic_id).execution_scope, f"root:{self.root}")

    def test_lane_archive_refuses_pending_work_and_preserves_binding(self) -> None:
        lane_root, branch = create_worktree(self.harness.registry.projects[0], "archive-busy")
        topic, session = self.topic_session(
            project_id="example-project",
            thread_id=118,
            agent_id="opencode",
            root=self.root,
        )
        self.state.register_lane(
            lane_id="archive-busy",
            project_id="example-project",
            worktree_path=lane_root,
            branch_name=branch,
        )
        self.state.bind_lane("archive-busy", topic.topic_id)
        self.enqueue(topic, session, 818)

        with self.assertRaisesRegex(RuntimeError, "active or unresolved"):
            self.state.archive_lane("archive-busy")

        self.assertEqual(self.state.get_lane("archive-busy")["status"], "active")
        self.assertEqual(
            self.state.get_topic(topic.topic_id).execution_scope,
            f"root:{lane_root}",
        )

    def test_lane_binding_refuses_an_existing_provider_session(self) -> None:
        lane_root, branch = create_worktree(self.harness.registry.projects[0], "bound-session")
        topic, session = self.topic_session(
            project_id="example-project",
            thread_id=122,
            agent_id="opencode",
            root=self.root,
        )
        self.state.bind_provider_session(session.session_id, "existing-provider-session", None)
        self.state.register_lane(
            lane_id="bound-session",
            project_id="example-project",
            worktree_path=lane_root,
            branch_name=branch,
        )

        with self.assertRaisesRegex(RuntimeError, "active or unresolved"):
            self.state.bind_lane("bound-session", topic.topic_id)


if __name__ == "__main__":
    unittest.main()
