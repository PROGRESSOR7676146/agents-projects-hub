from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.registry import ExecutionRootError
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.topic_execution import resolve_topic_execution_root
from tests.fault_matrix_support import FaultMatrixHarness
from tests.git_fixtures import init_git_root
from tests.test_bounded_concurrency import CapturingAdapter


class LegacyScopeEvidenceTests(unittest.TestCase):
    def test_normalization_refusal_closes_startup_connection(self) -> None:
        for kind in ("controller", "worker"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                harness = FaultMatrixHarness(Path(directory))
                state = HubState.open(harness.config.state_path)
                try:
                    with (
                        patch.object(HubState, "open", return_value=state),
                        patch.object(
                            state,
                            "reconcile_legacy_execution_scopes",
                            side_effect=StateError("fictional conflict"),
                        ),
                        patch.object(state, "close", wraps=state.close) as close,
                        patch(
                            "hermes_codex_router.service.load_registry",
                            return_value=harness.registry,
                        ),
                    ):
                        with self.assertRaisesRegex(StateError, "fictional conflict"):
                            if kind == "controller":
                                ProjectHubService(harness.config)
                            else:
                                ExternalQueueWorker(
                                    harness.config, "opencode", registry=harness.registry
                                )
                        close.assert_called_once()
                finally:
                    state.close()

    def test_known_id_cannot_move_retained_ownership_away_from_saved_root(self) -> None:
        for schema in (25, 27):
            for ownership in ("local", "terminal", "leased", "executing", "indeterminate"):
                with self.subTest(schema=schema, ownership=ownership):
                    self.check_saved_root(schema, ownership)

    def check_saved_root(self, schema: int, ownership: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            harness.config = replace(harness.config, max_parallel_roots=2)
            state = HubState.open(harness.config.state_path)
            project = harness.registry.projects[0]
            root = project.root
            old = state.observe_topic(
                project_id=project.project_id,
                chat_id=harness.chat_id,
                thread_id=71,
                title="Fictional retained topic",
                execution_root=root,
            )
            session = state.activate_agent(old.topic_id, "codex", "fictional", "high")
            state.bind_provider_session(session.session_id, "fictional-thread", None)
            if ownership in ("local", "terminal", "leased"):
                # Real schema-25 immutable origin, not an inferred current cwd.
                with state._connection:
                    state._connection.execute(
                        """INSERT INTO codex_session_origins
                           (session_id, provider_thread_id, project_id, canonical_root,
                            model_provider, created_at, activation_message_id)
                           VALUES (?, 'fictional-thread', ?, ?, 'openai', 'fictional', 1)""",
                        (session.session_id, project.project_id, str(root)),
                    )
            if ownership in ("local", "terminal"):
                state.set_writer_mode(session.session_id, ownership)
            else:
                job, _ = state.enqueue_provider_job(
                    idempotency_key="fictional:71",
                    chat_id=old.chat_id,
                    message_id=71,
                    topic_id=old.topic_id,
                    agent_id="codex",
                    session_id=session.session_id,
                    session_generation=session.generation,
                    model=session.model,
                    effort=session.effort,
                    payload_text="Fictional retained task",
                )
                lease = state.lease_provider_job("codex", "fictional-lost-worker")
                assert lease is not None and lease.lease_token is not None
                if ownership != "leased":
                    state.mark_provider_job_executing(job.job_id, lease.lease_token)
                    journal = ExecutionJournal(state)
                    journal.record_thread(job.job_id, lease.lease_token, "fictional-thread", root)
                    journal.record_turn(job.job_id, lease.lease_token, "fictional-turn")
                if ownership == "indeterminate":
                    state.terminate_provider_job_with_notice(
                        job.job_id,
                        lease.lease_token,
                        status="indeterminate",
                        error_class="ambiguous_execution",
                        error_code="fictional_loss",
                        sender_agent_id="codex",
                        telegram_html="Fictional uncertainty",
                    )
            with state._connection:
                state._connection.execute(
                    "UPDATE topics SET execution_scope=? WHERE topic_id=?",
                    ("project:example-project", old.topic_id),
                )
            preserved_tables = (
                "agent_sessions",
                "provider_jobs",
                "provider_execution_checkpoints",
                "codex_session_origins",
                "provider_job_resolutions",
                "telegram_outbox",
            )
            before = {
                table: state._connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in preserved_tables
            }
            state.close()
            if schema == 25:
                connection = sqlite3.connect(harness.config.state_path)
                with connection:
                    connection.execute("DROP INDEX topics_execution_scope")
                    connection.execute("ALTER TABLE topics DROP COLUMN execution_scope")
                    connection.execute("DROP INDEX worktree_lanes_one_active_topic")
                    connection.execute("DROP TABLE execution_scheduler_grants")
                    connection.execute("DROP TABLE execution_scheduler_workers")
                    connection.execute("PRAGMA user_version=25")
                connection.close()
            state = HubState.open(harness.config.state_path)
            replacement = Path(directory) / "fictional-replacement"
            init_git_root(replacement)
            harness.registry = ProjectRegistry(
                1,
                (Path(directory),),
                (
                    replace(project, root=replacement),
                    Project("historical-alias", "Fictional alias", "Fictional", root),
                ),
            )
            # Worker startup precedes any Controller observation of the old topic.
            adapter = CapturingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertEqual(state.schema_version, 27)
                self.assertEqual(state.get_topic(old.topic_id).execution_scope, f"root:{root}")
                for table in preserved_tables:
                    self.assertEqual(
                        state._connection.execute(f"SELECT * FROM {table}").fetchall(),
                        before[table],
                    )
                with self.assertRaises(ExecutionRootError):
                    resolve_topic_execution_root(
                        state, harness.registry, state.get_topic(old.topic_id)
                    )
                for number, project_id in ((72, "historical-alias"), (73, project.project_id)):
                    peer_root = root if number == 72 else replacement
                    topic = state.observe_topic(
                        project_id=project_id,
                        chat_id=harness.chat_id,
                        thread_id=number,
                        title="Fictional independent topic",
                        execution_root=peer_root,
                    )
                    peer = state.activate_agent(topic.topic_id, "opencode", "fictional", "high")
                    state.enqueue_provider_job(
                        idempotency_key=f"fictional:{number}",
                        chat_id=topic.chat_id,
                        message_id=number,
                        topic_id=topic.topic_id,
                        agent_id=peer.agent_id,
                        session_id=peer.session_id,
                        session_generation=peer.generation,
                        model=peer.model,
                        effort=peer.effort,
                        payload_text="Fictional peer task",
                    )
                self.assertTrue(worker.run_cycle())
                self.assertEqual(adapter.cwds, [replacement])
                if ownership not in ("local", "terminal"):
                    self.assertEqual(
                        state.provider_jobs_for_topic(old.topic_id)[0].status, ownership
                    )
            finally:
                worker.close()
                state.close()

    def test_conflicting_saved_evidence_rolls_back_all_scope_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            peer = HubState.open(Path(directory) / "state.db")
            try:
                for number in (1, 2):
                    topic = state.observe_topic(
                        project_id=f"example-{number}",
                        chat_id=-1001234567890,
                        thread_id=number,
                        title="Fictional legacy topic",
                    )
                    if number == 2:
                        for label in ("old", "replacement"):
                            session = (
                                state.activate_agent(topic.topic_id, "codex", "fictional", "high")
                                if label == "old"
                                else state.new_active_session(topic.topic_id)
                            )
                            state.bind_provider_session(
                                session.session_id, f"fictional-{label}", None
                            )
                            with state._connection:
                                state._connection.execute(
                                    """INSERT INTO codex_session_origins
                                       (session_id, provider_thread_id, project_id, canonical_root,
                                        model_provider, created_at) VALUES (?, ?, ?, ?, 'openai', 'fictional')""",
                                    (
                                        session.session_id,
                                        f"fictional-{label}",
                                        topic.project_id,
                                        str(Path(directory) / label),
                                    ),
                                )
                before = list(peer._connection.execute("SELECT * FROM topics"))
                with self.assertRaisesRegex(StateError, "ambiguous legacy execution root evidence"):
                    state.reconcile_legacy_execution_scopes(
                        {
                            "example-1": Path(directory),
                            "example-2": Path(directory),
                        }
                    )
                self.assertEqual(list(peer._connection.execute("SELECT * FROM topics")), before)
            finally:
                peer.close()
                state.close()
