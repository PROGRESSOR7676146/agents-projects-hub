from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.session_adoption_state import CodexSessionOrigins
from hermes_codex_router.session_connect import (
    ConnectCandidate,
    ConnectWorkflow,
    SessionConnectStore,
)
from hermes_codex_router.state import HubState, SessionRecord, StateError


class SessionConnectPlaceholderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = HubState.open(self.root / "state.db")
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        self.store = SessionConnectStore(self.state)
        self.source = ConnectCandidate("example-candidate", "saved-thread", "Saved session", 10)

    def _enqueue(self, session: SessionRecord, message_id: int = 10) -> str:
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"example:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=session.provider_session_id,
            model=session.model,
            effort=session.effort,
            payload_text="Example work",
        )
        return job.job_id

    def _historical_placeholder(
        self, *, resolve: bool = True, deliver: bool = True
    ) -> SessionRecord:
        old = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        old = self.state.bind_provider_session(old.session_id, "old-thread", None)
        job_id = self._enqueue(old)
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job_id, lease.lease_token)
        journal = ExecutionJournal(self.state)
        journal.record_thread(job_id, lease.lease_token, "old-thread", self.root)
        journal.record_turn(job_id, lease.lease_token, "old-turn")
        self.state.terminate_provider_job_with_notice(
            job_id,
            lease.lease_token,
            status="indeterminate",
            error_class="provider_failed",
            error_code="example_failure",
            sender_agent_id="codex",
            telegram_html="Example failed turn",
            terminal_turn_status="failed",
        )
        if deliver:
            outbox = self.state.lease_telegram_outbox("codex", "example-sender")
            assert outbox is not None and outbox.lease_token is not None
            self.state.mark_telegram_outbox_delivered(
                outbox.outbox_id, outbox.lease_token, telegram_message_id=20
            )
        if resolve:
            self.state.resolve_indeterminate_job(job_id, "acknowledged")
        placeholder = self.state.new_active_session(self.topic.topic_id)
        self.assertIsNone(placeholder.provider_session_id)
        self.state.ensure_satellite(self.topic.topic_id, "opencode", "example-model", "high")
        return placeholder

    def _select(self, entry: str) -> ConnectWorkflow:
        if entry == "code":
            code = self.store.issue_code(
                owner_user_id=42,
                project_id="example-project",
                canonical_root=self.root,
                source=self.source,
                model="example-model",
                effort="high",
            )
            result = self.store.redeem_code_topic(
                owner_user_id=42,
                code=code.code,
                project_id="example-project",
                canonical_root=self.root,
                chat_id=self.topic.chat_id,
                thread_id=self.topic.thread_id,
            )
            assert result.workflow is not None
            return result.workflow
        if entry == "topic":
            workflow = self.store.start_topic(
                owner_user_id=42,
                project_id="example-project",
                canonical_root=self.root,
                chat_id=self.topic.chat_id,
                thread_id=self.topic.thread_id,
                model="example-model",
                effort="high",
            )
        else:
            workflow = self.store.start_direct(
                owner_user_id=42,
                projects=(("example-project", self.root, "Example"),),
                model="example-model",
                effort="high",
            )
            self.store.select_project(
                42, self.store.options(workflow.workflow_id, "project")[0].option_id
            )
        leased = self.store.lease_worker("example-worker")
        assert leased is not None
        source = replace(self.source, candidate_id=f"example-{entry}")
        candidates = self.store.finish_discovery(
            workflow.workflow_id, leased.lease_token, (source,)
        )
        selected = self.store.select_candidate(42, candidates[0].candidate_id)
        if entry == "direct":
            destinations = self.store.prepare_destinations(
                42, workflow.workflow_id, chat_id=self.topic.chat_id
            )
            selected = self.store.select_destination(42, destinations[0].option_id)
        return selected

    def _history(self) -> dict[str, list[tuple[object, ...]]]:
        return {
            table: [tuple(row) for row in self.state._connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "provider_jobs",
                "provider_execution_checkpoints",
                "provider_turn_terminal_evidence",
                "provider_job_resolutions",
                "telegram_outbox",
                "telegram_outbox_parts",
            )
        }

    def _check_replacement(self, entry: str) -> None:
        placeholder = self._historical_placeholder()
        sessions = self.state._connection.execute(
            "SELECT session_id FROM agent_sessions"
        ).fetchall()
        retained = [
            self.state.get_session(row[0]) for row in sessions if row[0] != placeholder.session_id
        ]
        history = self._history()
        workflow = self._select(entry)
        self.assertEqual(workflow.expected_session_id, placeholder.session_id)
        self.assertEqual(workflow.replaces_session_id, placeholder.session_id)
        self.assertIn("будет архивирована", self.store.confirmation_text(workflow))
        self.store.request_activation(42, workflow.workflow_id)
        leased = self.store.lease_worker("example-worker")
        assert leased is not None
        self.store.prepare_marker(workflow.workflow_id, leased.lease_token)
        marker = self.store.lease_outbox("example-sender")
        assert marker is not None
        self.assertEqual(marker.kind, "activation_marker")
        completed = self.store.complete_marker(marker, telegram_message_id=120)
        assert completed.result_session_id is not None
        session = self.state.get_session(completed.result_session_id)
        self.assertEqual(completed.stage, "completed")
        self.assertNotEqual(session.session_id, placeholder.session_id)
        self.assertEqual(session.generation, placeholder.generation + 1)
        self.assertEqual(session.provider_session_id, self.source.provider_thread_id)
        self.assertEqual(session.writer_mode, "telegram")
        self.assertEqual(self.state.get_session(placeholder.session_id).status, "archived")
        origin = CodexSessionOrigins(self.state).require(session.session_id)
        self.assertEqual(origin.replaces_session_id, placeholder.session_id)
        self.assertEqual(origin.activation_message_id, 120)
        self.assertEqual(history, self._history())
        for previous in retained:
            self.assertEqual(self.state.get_session(previous.session_id), previous)
        with self.assertRaisesRegex(StateError, "activation"):
            self._enqueue(session, 119)
        self._enqueue(session, 121)

    def test_topic_replaces_historical_placeholder_without_losing_history(self) -> None:
        self._check_replacement("topic")

    def test_direct_replaces_historical_placeholder_without_losing_history(self) -> None:
        self._check_replacement("direct")

    def test_code_replaces_historical_placeholder_without_losing_history(self) -> None:
        self._check_replacement("code")

    def test_truly_empty_destination_has_no_replacement_warning(self) -> None:
        for entry in ("topic", "direct", "code"):
            with self.subTest(entry=entry):
                workflow = self._select(entry)
                self.assertIsNone(workflow.expected_session_id)
                self.assertIsNone(workflow.replaces_session_id)
                self.assertNotIn("будет архивирована", self.store.confirmation_text(workflow))

    def test_proven_failed_history_still_requires_owner_resolution(self) -> None:
        placeholder = self._historical_placeholder(resolve=False)
        workflow = self._select("topic")
        with self.assertRaisesRegex(StateError, "unresolved_work"):
            self.store.request_activation(42, workflow.workflow_id)
        self.assertEqual(self.state.active_session(self.topic.topic_id), placeholder)

    def test_unfinished_failure_delivery_still_blocks_replacement(self) -> None:
        placeholder = self._historical_placeholder(deliver=False)
        workflow = self._select("topic")
        with self.assertRaisesRegex(StateError, "pending_delivery"):
            self.store.request_activation(42, workflow.workflow_id)
        self.assertEqual(self.state.active_session(self.topic.topic_id), placeholder)

    def test_placeholder_changed_after_confirmation_is_not_replaced(self) -> None:
        self._historical_placeholder()
        workflow = self._select("topic")
        changed = self.state.new_active_session(self.topic.topic_id)
        with self.assertRaisesRegex(StateError, "target_changed"):
            self.store.request_activation(42, workflow.workflow_id)
        self.assertEqual(self.state.active_session(self.topic.topic_id), changed)

    def test_new_work_after_confirmation_blocks_marker_without_archiving(self) -> None:
        placeholder = self._historical_placeholder()
        workflow = self._select("topic")
        self.store.request_activation(42, workflow.workflow_id)
        self._enqueue(placeholder, 121)
        leased = self.store.lease_worker("example-worker")
        assert leased is not None
        with self.assertRaisesRegex(StateError, "target_busy"):
            self.store.prepare_marker(workflow.workflow_id, leased.lease_token)
        self.assertEqual(self.state.active_session(self.topic.topic_id), placeholder)

    def test_omitting_replacement_still_refuses_historical_placeholder(self) -> None:
        self._historical_placeholder()
        workflow = self._select("topic")
        request = replace(self.store._adoption_request(workflow), replaces_session_id=None)
        with self.assertRaisesRegex(StateError, "target_not_empty"):
            CodexSessionOrigins(self.state).preview(request)

    def test_existing_workflow_does_not_acquire_replacement_implicitly(self) -> None:
        self._historical_placeholder()
        workflow = self._select("topic")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE session_connect_workflows SET replaces_session_id=NULL WHERE workflow_id=?",
                (workflow.workflow_id,),
            )
        with self.assertRaisesRegex(StateError, "target_not_empty"):
            self.store.request_activation(42, workflow.workflow_id)
        self.assertIsNone(self.store.get(workflow.workflow_id).replaces_session_id)
