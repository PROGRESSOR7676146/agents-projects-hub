from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router import cli
from hermes_codex_router.cli import _lane_command
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.registry import validate_execution_root
from hermes_codex_router.state import HubState, SessionRecord, StateError, TopicRecord
from hermes_codex_router.worktrees import create_worktree
from tests.fault_matrix_support import FaultMatrixHarness
from tests.git_fixtures import init_git_root


class LaneArchiveTests(unittest.TestCase):
    def test_cli_archive_retains_canonical_destination_after_restart_with_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            root = harness.registry.projects[0].root
            lane_root, branch = create_worktree(harness.registry.projects[0], "historical")
            state = HubState.open(harness.config.state_path, codex_permission_profile=None)
            try:
                topic = state.observe_topic(
                    project_id="example-project",
                    chat_id=harness.chat_id,
                    thread_id=77,
                    title="Fictional archive topic",
                    execution_root=root,
                )
                state.register_lane(
                    lane_id="historical",
                    project_id=topic.project_id,
                    worktree_path=lane_root,
                    branch_name=branch,
                    topic_id=topic.topic_id,
                )
                session = state.activate_agent(topic.topic_id, "codex", "fictional", "high")
                state.bind_provider_session(session.session_id, "fictional-history", None)
                with state._connection:
                    state._connection.execute(
                        """INSERT INTO codex_session_origins
                           (session_id, provider_thread_id, project_id, canonical_root,
                            model_provider, created_at)
                           VALUES (?, 'fictional-history', ?, ?, 'openai', 'fictional')""",
                        (session.session_id, topic.project_id, str(lane_root)),
                    )
                state.new_active_session(topic.topic_id)
                before = tuple(state._connection.execute("SELECT * FROM codex_session_origins"))
                with (
                    patch("hermes_codex_router.cli.load_hub_config", return_value=harness.config),
                    patch("hermes_codex_router.cli._print"),
                ):
                    self.assertEqual(
                        _lane_command(
                            argparse.Namespace(
                                config=Path(directory) / "fictional-config.json",
                                lane_command="archive",
                                lane="historical",
                            )
                        ),
                        0,
                    )
            finally:
                state.close()
            reopened = HubState.open(harness.config.state_path, codex_permission_profile=None)
            try:
                self.assertEqual(
                    reopened.reconcile_legacy_execution_scopes({topic.project_id: root}), 0
                )
                self.assertEqual(reopened.get_topic(topic.topic_id).execution_scope, f"root:{root}")
                self.assertEqual(
                    tuple(reopened._connection.execute("SELECT * FROM codex_session_origins")),
                    before,
                )
            finally:
                reopened.close()


class LaneArchiveBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.harness = FaultMatrixHarness(self.base)
        self.root = self.harness.registry.projects[0].root
        self.state = HubState.open(self.harness.config.state_path, codex_permission_profile=None)
        self.topic, self.session = self.topic_session(77, self.root)
        self.lane_root, branch = create_worktree(self.harness.registry.projects[0], "historical")
        self.state.register_lane(
            lane_id="historical",
            project_id=self.topic.project_id,
            worktree_path=self.lane_root,
            branch_name=branch,
            topic_id=self.topic.topic_id,
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def topic_session(self, thread: int, root: Path) -> tuple[TopicRecord, SessionRecord]:
        topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=self.harness.chat_id,
            thread_id=thread,
            title="Fictional archive topic",
            execution_root=root,
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "fictional", "high")
        return self.state.get_topic(topic.topic_id), session

    def archive(self) -> None:
        self.state.archive_lane(
            "historical",
            project_id=self.topic.project_id,
            project_root=self.root,
        )

    def cli_archive(self, lane_id: str = "historical") -> int:
        with (
            patch("hermes_codex_router.cli.load_hub_config", return_value=self.harness.config),
            patch("hermes_codex_router.cli._print"),
        ):
            return cli.main(
                [
                    "lane",
                    "archive",
                    str(self.base / "fictional-config.json"),
                    "--lane",
                    lane_id,
                ]
            )

    def dump(self) -> str:
        return "\n".join(self.state._connection.iterdump())

    def enqueue(self, topic: TopicRecord, session: SessionRecord):
        return self.state.enqueue_provider_job(
            idempotency_key=f"archive:{topic.topic_id}",
            chat_id=topic.chat_id,
            message_id=topic.thread_id + 100,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Fictional archive work",
        )[0]

    def origin(self, session: SessionRecord, root: Path, thread: str) -> None:
        with self.state._connection:
            self.state._connection.execute(
                """INSERT INTO codex_session_origins
                   (session_id, provider_thread_id, project_id, canonical_root,
                    model_provider, created_at) VALUES (?, ?, ?, ?, 'openai', 'fictional')""",
                (
                    session.session_id,
                    thread,
                    self.state.get_topic(session.topic_id).project_id,
                    str(root),
                ),
            )

    def assert_refused_unchanged(self, pattern: str = "active or unresolved") -> None:
        before = self.dump()
        with self.assertRaisesRegex(StateError, pattern):
            self.archive()
        self.assertEqual(self.dump(), before)

    def test_completed_checkpoint_and_result_survive_archive_and_restart(self) -> None:
        job = self.enqueue(self.topic, self.session)
        lease = self.state.lease_provider_job("codex", "fictional-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(self.state)
        journal.record_thread(job.job_id, lease.lease_token, "fictional-history", self.lane_root)
        journal.record_turn(job.job_id, lease.lease_token, "fictional-turn")
        journal.record_completion(job.job_id, lease.lease_token, "Fictional saved completion")
        self.origin(self.session, self.lane_root, "fictional-history")
        self.state.commit_provider_result(
            job.job_id,
            lease.lease_token,
            visible_response="Fictional saved completion",
            sender_agent_id="codex",
            telegram_html="Fictional saved completion",
        )
        sender = self.harness.sender()
        try:
            self.assertTrue(sender._deliver_one("codex"))
        finally:
            sender.close()
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "completed")
        self.state.new_active_session(self.topic.topic_id)
        before = journal.read(job.job_id)
        origins = tuple(self.state._connection.execute("SELECT * FROM codex_session_origins"))
        self.archive()
        self.state.close()
        self.state = HubState.open(self.harness.config.state_path, codex_permission_profile=None)
        self.assertEqual(
            self.state.reconcile_legacy_execution_scopes({self.topic.project_id: self.root}), 0
        )
        self.assertEqual(
            self.state.get_topic(self.topic.topic_id).execution_scope, f"root:{self.root}"
        )
        self.assertEqual(ExecutionJournal(self.state).read(job.job_id), before)
        self.assertEqual(
            tuple(self.state._connection.execute("SELECT * FROM codex_session_origins")), origins
        )
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "completed")
        self.assertTrue(self.lane_root.is_dir())

    def test_multiple_historical_roots_do_not_enter_legacy_normalization(self) -> None:
        self.origin(self.session, self.lane_root, "fictional-first")
        second = self.state.new_active_session(self.topic.topic_id)
        self.origin(second, self.root, "fictional-second")
        self.state.new_active_session(self.topic.topic_id)
        origins = tuple(self.state._connection.execute("SELECT * FROM codex_session_origins"))
        self.archive()
        self.state.close()
        self.state = HubState.open(self.harness.config.state_path, codex_permission_profile=None)
        self.assertEqual(
            self.state.reconcile_legacy_execution_scopes({self.topic.project_id: self.root}), 0
        )
        self.assertEqual(
            self.state.get_topic(self.topic.topic_id).execution_scope, f"root:{self.root}"
        )
        self.assertEqual(
            tuple(self.state._connection.execute("SELECT * FROM codex_session_origins")), origins
        )

    def check_destination_work(self, status: str) -> None:
        peer, session = self.topic_session(78, self.root)
        job = self.enqueue(peer, session)
        if status != "queued":
            lease = self.state.lease_provider_job("codex", "fictional-peer")
            assert lease is not None and lease.lease_token is not None
            if status != "leased":
                self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
            if status == "result_ready":
                self.state.commit_provider_result(
                    job.job_id,
                    lease.lease_token,
                    visible_response="Fictional peer result",
                    sender_agent_id="codex",
                    telegram_html="Fictional peer result",
                )
            elif status == "indeterminate":
                self.state.terminate_provider_job_with_notice(
                    job.job_id,
                    lease.lease_token,
                    status="indeterminate",
                    error_class="transport",
                    error_code="fictional_unknown",
                    sender_agent_id="codex",
                    telegram_html="Fictional uncertainty",
                )
        self.assertEqual(self.state.get_provider_job(job.job_id).status, status)
        self.assert_refused_unchanged()

    def test_destination_queued_work_blocks_archive(self) -> None:
        self.check_destination_work("queued")

    def test_destination_leased_work_blocks_archive(self) -> None:
        self.check_destination_work("leased")

    def test_destination_executing_work_blocks_archive(self) -> None:
        self.check_destination_work("executing")

    def test_destination_undelivered_result_blocks_archive(self) -> None:
        self.check_destination_work("result_ready")

    def test_destination_uncertainty_blocks_archive(self) -> None:
        self.check_destination_work("indeterminate")

    def test_destination_dispatch_blocks_archive(self) -> None:
        peer, _ = self.topic_session(78, self.root)
        self.state.start_dispatch(
            chat_id=peer.chat_id,
            message_id=178,
            topic_id=peer.topic_id,
            agent_id="codex",
        )
        self.assert_refused_unchanged()

    def test_destination_local_and_terminal_writer_block_archive(self) -> None:
        _, session = self.topic_session(78, self.root)
        for mode in ("local", "terminal"):
            with self.subTest(mode=mode):
                self.state.set_writer_mode(session.session_id, mode)
                self.assert_refused_unchanged()
                self.state.set_writer_mode(session.session_id, "telegram")

    def test_idle_destination_active_and_satellite_provider_bindings_survive_archive(self) -> None:
        peer, session = self.topic_session(78, self.root)
        self.state.bind_provider_session(session.session_id, "fictional-peer", None)
        self.origin(session, self.root, "fictional-peer")
        active = self.state.activate_agent(peer.topic_id, "opencode", "fictional", "high")
        self.state.bind_provider_session(active.session_id, "fictional-active-peer", None)
        peer = self.state.get_topic(peer.topic_id)
        sessions = tuple(self.state._connection.execute("SELECT * FROM agent_sessions"))
        origins = tuple(self.state._connection.execute("SELECT * FROM codex_session_origins"))
        self.archive()
        self.assertEqual(self.state.get_session(session.session_id).status, "satellite")
        self.assertEqual(self.state.get_topic(peer.topic_id), peer)
        self.assertEqual(
            tuple(self.state._connection.execute("SELECT * FROM agent_sessions")), sessions
        )
        self.assertEqual(
            tuple(self.state._connection.execute("SELECT * FROM codex_session_origins")), origins
        )

    def test_source_provider_binding_still_blocks_archive(self) -> None:
        self.state.bind_provider_session(self.session.session_id, "fictional-source", None)
        self.assert_refused_unchanged()

    def test_idle_legacy_provider_bindings_survive_archive_in_all_legacy_forms(self) -> None:
        peers = []
        for thread, scope in ((78, "project:example-project"), (79, None), (80, "")):
            peer, session = self.topic_session(thread, self.root)
            self.state.bind_provider_session(session.session_id, f"fictional-peer-{thread}", None)
            with self.state._connection:
                self.state._connection.execute(
                    "UPDATE topics SET execution_scope=? WHERE topic_id=?", (scope, peer.topic_id)
                )
            peers.append(
                (self.state.get_topic(peer.topic_id), self.state.get_session(session.session_id))
            )
        self.archive()
        for peer, session in peers:
            self.assertEqual(self.state.get_topic(peer.topic_id), peer)
            self.assertEqual(self.state.get_session(session.session_id), session)

    def test_destination_check_observes_other_connection_commit(self) -> None:
        peer, session = self.topic_session(78, self.root)
        other = HubState.open(self.harness.config.state_path, codex_permission_profile=None)
        try:
            other.set_writer_mode(session.session_id, "local")
            self.assertEqual(other.get_topic(peer.topic_id).execution_scope, f"root:{self.root}")
            self.assert_refused_unchanged()
        finally:
            other.close()

    def test_destination_other_project_identity_still_blocks_same_root(self) -> None:
        peer = self.state.observe_topic(
            project_id="historical-registration",
            chat_id=self.harness.chat_id,
            thread_id=78,
            title="Fictional prior registration",
            execution_root=self.root,
        )
        session = self.state.activate_agent(peer.topic_id, "opencode", "fictional", "high")
        self.state.set_writer_mode(session.session_id, "local")
        self.assert_refused_unchanged()

    def test_same_project_busy_legacy_scopes_block_destination_archive(self) -> None:
        peer, session = self.topic_session(78, self.root)
        self.enqueue(peer, session)
        for scope in ("project:example-project", None, ""):
            with self.subTest(scope=scope):
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                        (scope, peer.topic_id),
                    )
                self.assert_refused_unchanged()

    def test_foreign_legacy_origin_retains_destination_writer_exclusion(self) -> None:
        peer = self.state.observe_topic(
            project_id="historical-registration",
            chat_id=self.harness.chat_id,
            thread_id=78,
            title="Fictional legacy registration",
        )
        session = self.state.activate_agent(peer.topic_id, "codex", "fictional", "high")
        self.state.bind_provider_session(session.session_id, "fictional-legacy", None)
        self.origin(session, self.root, "fictional-legacy")
        self.state.set_writer_mode(session.session_id, "local")
        self.assert_refused_unchanged()

    def test_foreign_legacy_checkpoint_retains_destination_uncertainty(self) -> None:
        peer = self.state.observe_topic(
            project_id="historical-registration",
            chat_id=self.harness.chat_id,
            thread_id=78,
            title="Fictional legacy registration",
        )
        session = self.state.activate_agent(peer.topic_id, "codex", "fictional", "high")
        job = self.enqueue(peer, session)
        lease = self.state.lease_provider_job("codex", "fictional-peer")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(self.state)
        journal.record_thread(job.job_id, lease.lease_token, "fictional-legacy", self.root)
        self.state.terminate_provider_job_with_notice(
            job.job_id,
            lease.lease_token,
            status="indeterminate",
            error_class="transport",
            error_code="fictional_unknown",
            sender_agent_id="codex",
            telegram_html="Fictional legacy uncertainty",
        )
        self.assert_refused_unchanged()

    def test_foreign_legacy_writer_without_root_evidence_blocks_archive(self) -> None:
        peer = self.state.observe_topic(
            project_id="unknown-registration",
            chat_id=self.harness.chat_id,
            thread_id=78,
            title="Fictional unknown legacy root",
        )
        session = self.state.activate_agent(peer.topic_id, "opencode", "fictional", "high")
        self.state.set_writer_mode(session.session_id, "local")
        self.assert_refused_unchanged()

    def test_idle_legacy_topic_is_preserved_without_blocking_archive(self) -> None:
        peer = self.state.observe_topic(
            project_id="unknown-registration",
            chat_id=self.harness.chat_id,
            thread_id=78,
            title="Fictional idle legacy topic",
        )
        session = self.state.activate_agent(peer.topic_id, "opencode", "fictional", "high")
        self.state.bind_provider_session(session.session_id, "fictional-idle-legacy", None)
        peer = self.state.get_topic(peer.topic_id)
        session = self.state.get_session(session.session_id)
        self.archive()
        self.assertEqual(self.state.get_topic(peer.topic_id), peer)
        self.assertEqual(self.state.get_session(session.session_id), session)

    def test_busy_canonical_independent_root_does_not_block_archive(self) -> None:
        independent = self.base / "independent"
        init_git_root(independent)
        peer, session = self.topic_session(78, independent)
        job = self.enqueue(peer, session)
        self.archive()
        self.assertEqual(self.state.get_topic(peer.topic_id), peer)
        self.assertEqual(self.state.get_provider_job(job.job_id), job)
        self.assertEqual(self.state.get_session(session.session_id), session)

    def test_same_project_legacy_local_writer_blocks_all_legacy_forms(self) -> None:
        peer, session = self.topic_session(78, self.root)
        self.state.set_writer_mode(session.session_id, "local")
        for scope in ("project:example-project", None, ""):
            with self.subTest(scope=scope):
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                        (scope, peer.topic_id),
                    )
                self.assert_refused_unchanged()

    def test_bound_archive_refuses_missing_or_mismatched_destination(self) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {},
            {"project_id": self.topic.project_id},
            {"project_root": self.root},
            {"project_id": "different-project", "project_root": self.root},
            {"project_id": self.topic.project_id, "project_root": Path("relative")},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                before = self.dump()
                with self.assertRaisesRegex(StateError, "validated project root"):
                    self.state.archive_lane("historical", **kwargs)
                self.assertEqual(self.dump(), before)

    def test_archive_refuses_changed_source_scope_and_project(self) -> None:
        for column, value in (
            ("execution_scope", f"root:{self.root}"),
            ("project_id", "different-project"),
        ):
            with self.subTest(column=column):
                original = self.state.get_topic(self.topic.topic_id)
                with self.state._connection:
                    self.state._connection.execute(
                        f"UPDATE topics SET {column}=? WHERE topic_id=?",
                        (value, self.topic.topic_id),
                    )
                self.assert_refused_unchanged("scope mismatch|validated project root")
                with self.state._connection:
                    self.state._connection.execute(
                        f"UPDATE topics SET {column}=? WHERE topic_id=?",
                        (getattr(original, column), self.topic.topic_id),
                    )

    def test_topic_update_fault_rolls_back_lane_archive(self) -> None:
        self.state._connection.execute(
            """CREATE TRIGGER fictional_archive_fault BEFORE UPDATE OF execution_scope ON topics
               BEGIN SELECT RAISE(ABORT, 'fictional archive fault'); END"""
        )
        before = self.dump()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fictional archive fault"):
            self.archive()
        self.assertEqual(self.dump(), before)

    def test_cli_invalid_registration_and_git_root_do_not_archive(self) -> None:
        original = self.harness.config.registry_path.read_text()
        invalid_root = self.base / "not-a-git-root"
        (invalid_root / ".git").mkdir(parents=True)
        for kind in ("disabled", "missing", "not_git", "outside_allowed"):
            with self.subTest(kind=kind):
                document = json.loads(original)
                if kind == "disabled":
                    document["projects"][0]["enabled"] = False
                elif kind == "missing":
                    document["projects"] = []
                elif kind == "not_git":
                    document["projects"][0]["root"] = str(invalid_root)
                else:
                    document["allowed_roots"] = [str(self.lane_root)]
                self.harness.config.registry_path.write_text(json.dumps(document))
                before = self.dump()
                self.assertEqual(self.cli_archive(), 2)
                self.assertEqual(self.dump(), before)

    def test_cli_archive_uses_current_validated_registration(self) -> None:
        destination = self.base / "registered-destination"
        init_git_root(destination)
        document = json.loads(self.harness.config.registry_path.read_text())
        document["projects"][0]["root"] = str(destination)
        self.harness.config.registry_path.write_text(json.dumps(document))
        self.assertEqual(self.cli_archive(), 0)
        self.assertEqual(
            self.state.get_topic(self.topic.topic_id).execution_scope, f"root:{destination}"
        )

    def test_cli_unbound_archive_does_not_require_registry_or_git_validation(self) -> None:
        unbound_root = self.base / "unbound"
        unbound_root.mkdir()
        self.state.register_lane(
            lane_id="unbound",
            project_id="unavailable-registration",
            worktree_path=unbound_root,
            branch_name="lane/unbound",
        )
        before = self.state.get_topic(self.topic.topic_id)
        with (
            patch(
                "hermes_codex_router.cli.registry_lock", side_effect=AssertionError("registry lock")
            ),
            patch(
                "hermes_codex_router.cli.validate_execution_root",
                side_effect=AssertionError("Git validation"),
            ),
        ):
            self.assertEqual(self.cli_archive("unbound"), 0)
        self.assertEqual(self.state.get_lane("unbound")["status"], "archived")
        self.assertEqual(self.state.get_topic(self.topic.topic_id), before)
        self.assertTrue(unbound_root.is_dir())

    def test_cli_registry_lock_covers_validation_and_archive_transaction(self) -> None:
        checked: list[str] = []
        opened: list[HubState] = []
        archive = HubState.archive_lane
        open_state = HubState.open
        lock_path = self.harness.config.registry_path.with_name(".projects.json.lock")

        def check_lock(phase: str) -> None:
            with lock_path.open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            checked.append(phase)

        def validate(registry, project):
            check_lock("validation")
            self.assertEqual(len(opened), 1)
            self.assertFalse(opened[0]._connection.in_transaction)
            return validate_execution_root(registry, project)

        def capture_state(*args: Any, **kwargs: Any) -> HubState:
            state = open_state(*args, **kwargs)
            opened.append(state)
            return state

        def archive_locked(state: HubState, lane_id: str, **kwargs: Any) -> None:
            check_lock("archive")
            archive(state, lane_id, **kwargs)

        with (
            patch("hermes_codex_router.cli.validate_execution_root", side_effect=validate),
            patch.object(HubState, "open", side_effect=capture_state),
            patch.object(HubState, "archive_lane", archive_locked),
        ):
            self.assertEqual(self.cli_archive(), 0)
        self.assertEqual(checked, ["validation", "archive"])
        self.assertEqual(
            self.state.get_topic(self.topic.topic_id).execution_scope, f"root:{self.root}"
        )


if __name__ == "__main__":
    unittest.main()
