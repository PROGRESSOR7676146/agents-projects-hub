from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.state import HubState
from hermes_codex_router.topic_execution import resolve_topic_execution_root
from hermes_codex_router.worktrees import create_worktree
from tests.fault_matrix_support import FaultMatrixHarness
from tests.test_bounded_concurrency import CapturingAdapter


class LaneWriterTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.harness = FaultMatrixHarness(Path(self.tempdir.name))
        self.controller = self.harness.controller()
        self.state = self.controller.state
        project = self.harness.registry.projects[0]
        self.root, branch = create_worktree(project, "transfer")
        self.controller.handle_update(self.harness.update(1, 77, "/menu"))
        topic = self.state.find_topic(self.harness.chat_id, 77)
        assert topic is not None
        self.state.register_lane(
            lane_id="transfer",
            project_id=project.project_id,
            worktree_path=self.root,
            branch_name=branch,
            topic_id=topic.topic_id,
        )
        self.topic = self.state.get_topic(topic.topic_id)
        self.session = self.state.activate_agent(topic.topic_id, "opencode", "fictional", "high")
        self.state.bind_provider_session(self.session.session_id, "fictional-lane-session", None)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def command(self, number: int, text: str) -> bool:
        return self.controller.handle_update(self.harness.update(number, 77, text))

    def test_invalid_lane_return_preserves_local_writer_and_enqueues_nothing(self) -> None:
        self.command(2, "/local")
        before = self.state.get_session(self.session.session_id)
        self.assertEqual(before.writer_mode, "local")
        self.root.rename(self.root.with_name(self.root.name + "-moved"))
        self.assertTrue(self.command(3, "/return"))
        self.assertEqual(self.state.get_session(self.session.session_id), before)
        self.assertEqual(self.state.provider_jobs_for_topic(self.topic.topic_id), ())

    def test_valid_return_summary_uses_same_session_and_lane_cwd(self) -> None:
        self.command(2, "/local")
        self.command(3, "/return")
        jobs = self.state.provider_jobs_for_topic(self.topic.topic_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].provider_session_id, "fictional-lane-session")
        self.assertEqual(self.state.get_session(self.session.session_id).writer_mode, "telegram")
        case = self

        class SummaryAdapter(CapturingAdapter):
            def run_turn(self, **kwargs):
                case.assertEqual(kwargs["session_id"], "fictional-lane-session")
                staging_dir = kwargs["staging_dir"]
                assert isinstance(staging_dir, Path)
                case.assertEqual(staging_dir.parent.parent.parent, case.root)
                result = super().run_turn(**kwargs)
                return replace(result, provider_session_id="fictional-lane-session")

        adapter = SummaryAdapter("opencode")
        worker = self.harness.worker("opencode", adapter)
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(adapter.cwds, [self.root])
            self.assertEqual(self.state.get_provider_job(jobs[0].job_id).status, "result_ready")
            self.assertEqual(
                self.state.get_session(self.session.session_id).provider_session_id,
                "fictional-lane-session",
            )
        finally:
            worker.close()

    def test_return_rejects_persisted_snapshot_change_after_filesystem_validation(self) -> None:
        self.command(2, "/local")
        self.check_interleaving("/return", "local")

    def test_local_rejects_persisted_snapshot_change_after_filesystem_validation(self) -> None:
        self.check_interleaving("/local", "telegram")

    def test_scope_lane_and_generation_drift_are_rechecked_inside_return_transaction(self) -> None:
        self.command(2, "/local")
        peer = HubState.open(self.harness.config.state_path)
        mutations = (
            (
                "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                "root:/fictional-drift",
                self.topic.execution_scope,
            ),
            (
                "UPDATE worktree_lanes SET worktree_path=? WHERE topic_id=?",
                "/fictional-drift",
                str(self.root),
            ),
            (
                "UPDATE agent_sessions SET generation=? WHERE topic_id=?",
                self.session.generation + 1,
                self.session.generation,
            ),
        )
        try:
            for number, (sql, changed, original) in enumerate(mutations, 10):
                with self.subTest(sql=sql):

                    def interleave(state, registry, topic):
                        root = resolve_topic_execution_root(state, registry, topic)
                        self.assertFalse(state._connection.in_transaction)
                        with peer._connection:
                            peer._connection.execute(sql, (changed, self.topic.topic_id))
                        return root

                    try:
                        with patch(
                            "hermes_codex_router.service.resolve_topic_execution_root",
                            side_effect=interleave,
                        ) as resolver:
                            self.assertTrue(self.command(number, "/return"))
                            resolver.assert_called_once()
                        self.assertEqual(
                            peer.get_session(self.session.session_id).writer_mode, "local"
                        )
                        self.assertEqual(peer.provider_jobs_for_topic(self.topic.topic_id), ())
                    finally:
                        with peer._connection:
                            peer._connection.execute(sql, (original, self.topic.topic_id))
        finally:
            peer.close()

    def test_summary_admission_fault_rolls_back_writer_and_queue_together(self) -> None:
        self.command(2, "/local")
        before = self.state.get_session(self.session.session_id)
        with self.state._connection:
            self.state._connection.execute(
                """CREATE TRIGGER fictional_admission_fault BEFORE INSERT ON provider_jobs
                   BEGIN SELECT RAISE(ABORT, 'fictional admission fault'); END"""
            )
        from hermes_codex_router.service import QueueAcceptanceError

        with self.assertRaises(QueueAcceptanceError):
            self.command(3, "/return")
        peer = HubState.open(self.harness.config.state_path)
        try:
            self.assertEqual(peer.get_session(self.session.session_id), before)
            self.assertEqual(peer.provider_jobs_for_topic(self.topic.topic_id), ())
        finally:
            peer.close()

    def check_interleaving(self, command: str, writer: str) -> None:
        peer = HubState.open(self.harness.config.state_path)
        try:

            def interleave(state, registry, topic):
                root = resolve_topic_execution_root(state, registry, topic)
                self.assertFalse(state._connection.in_transaction)
                # A real concurrent connection commits a provider-binding change
                # after validation. The ownership transaction must see it.
                peer.bind_provider_session(self.session.session_id, "fictional-replacement", None)
                return root

            with patch(
                "hermes_codex_router.service.resolve_topic_execution_root", side_effect=interleave
            ):
                self.assertTrue(self.command(3, command))
            self.assertEqual(
                peer.get_session(self.session.session_id).provider_session_id,
                "fictional-replacement",
            )
            self.assertEqual(peer.get_session(self.session.session_id).writer_mode, writer)
            self.assertEqual(peer.provider_jobs_for_topic(self.topic.topic_id), ())
        finally:
            peer.close()
