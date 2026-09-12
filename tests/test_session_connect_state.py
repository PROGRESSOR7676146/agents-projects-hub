from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.session_connect import ConnectCandidate, SessionConnectStore
from hermes_codex_router.state import HubState, StateError


class SessionConnectStateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = HubState.open(self.root / "state.db")
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example topic",
        )
        self.store = SessionConnectStore(self.state)

    def test_topic_workflow_discovers_selects_and_activates_without_return(self) -> None:
        workflow = self.store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
            model="gpt-5.6-sol",
            effort="high",
        )
        leased = self.store.lease_worker("worker-1")
        self.assertEqual(leased.workflow_id, workflow.workflow_id)
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (
                ConnectCandidate(
                    "candidate-1",
                    "example-thread",
                    "Сессия · 2026-09-12 10:00 · thread",
                    1_789_200_000,
                ),
            ),
        )
        selected = self.store.select_candidate(42, candidates[0].candidate_id)
        self.assertEqual(selected.stage, "confirming")
        requested = self.store.request_activation(42, selected.workflow_id)
        self.assertEqual(requested.stage, "activation_requested")
        leased = self.store.lease_worker("worker-1")
        self.store.prepare_marker(leased.workflow_id, leased.lease_token)
        outbox = self.store.lease_outbox("sender-1")
        self.assertEqual(outbox.kind, "activation_marker")
        completed = self.store.complete_marker(outbox, telegram_message_id=120)
        self.assertEqual(completed.stage, "completed")
        session = self.state.get_session(completed.result_session_id)
        self.assertEqual(session.writer_mode, "telegram")
        self.assertEqual(session.provider_session_id, "example-thread")
        origin = self.state._connection.execute(
            "SELECT activation_message_id FROM codex_session_origins WHERE session_id=?",
            (session.session_id,),
        ).fetchone()
        self.assertEqual(origin[0], 120)
        with self.assertRaisesRegex(StateError, "activation"):
            self.state.enqueue_provider_job(
                idempotency_key="old",
                chat_id=-1001234567890,
                message_id=119,
                topic_id=self.topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=session.provider_session_id,
                model=session.model,
                effort=session.effort,
                payload_text="Delayed input",
            )

    def test_marker_unknown_outcome_preserves_current_binding(self) -> None:
        previous = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        self.state.bind_provider_session(previous.session_id, "old-thread", None)
        workflow = self.store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
            model="gpt-5.6-sol",
            effort="high",
        )
        leased = self.store.lease_worker("worker-1")
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-2", "new-thread", "Сессия · thread", 1),),
        )
        selected = self.store.select_candidate(42, candidates[0].candidate_id)
        self.store.request_activation(42, selected.workflow_id)
        leased = self.store.lease_worker("worker-1")
        self.store.prepare_marker(leased.workflow_id, leased.lease_token)
        outbox = self.store.lease_outbox("sender-1")
        self.store.mark_marker_unknown(outbox, "telegram_outcome_unknown")
        self.assertEqual(
            self.state.active_session(self.topic.topic_id).session_id,
            previous.session_id,
        )
        self.assertEqual(self.store.get(workflow.workflow_id).stage, "marker_unknown")

    def test_direct_workflow_selects_project_and_existing_destination(self) -> None:
        workflow = self.store.start_direct(
            owner_user_id=42,
            projects=(("example-project", self.root, "Example project"),),
            model="gpt-5.6-sol",
            effort="high",
        )
        self.assertEqual(workflow.stage, "choosing_project")
        option = self.store.options(workflow.workflow_id, "project")[0]
        discovering = self.store.select_project(42, option.option_id)
        self.assertEqual(discovering.project_id, "example-project")
        self.assertEqual(discovering.stage, "discovering")

        leased = self.store.lease_worker("worker-1")
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-3", "saved-thread", "Сессия · saved", 10),),
        )
        choosing = self.store.select_candidate(42, candidates[0].candidate_id)
        self.assertEqual(choosing.stage, "choosing_destination")
        destinations = self.store.prepare_destinations(
            42, workflow.workflow_id, chat_id=-1001234567890
        )
        selected = self.store.select_destination(42, destinations[0].option_id)
        self.assertEqual(selected.stage, "confirming")
        self.assertEqual(selected.destination_thread_id, 77)

    def test_direct_workflow_records_created_topic_before_confirmation(self) -> None:
        workflow = self.store.start_direct(
            owner_user_id=42,
            projects=(("example-project", self.root, "Example project"),),
            model="gpt-5.6-sol",
            effort="high",
        )
        project = self.store.options(workflow.workflow_id, "project")[0]
        self.store.select_project(42, project.option_id)
        leased = self.store.lease_worker("worker-1")
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-4", "saved-thread", "Сессия · saved", 10),),
        )
        self.store.select_candidate(42, candidates[0].candidate_id)
        self.store.request_new_topic(42, workflow.workflow_id)
        self.store.begin_topic_creation(42, workflow.workflow_id)
        completed = self.store.complete_topic_creation(
            42,
            workflow.workflow_id,
            chat_id=-1001234567890,
            thread_id=88,
            title="Saved work",
        )
        self.assertEqual(completed.stage, "confirming")
        self.assertEqual(
            self.state.find_topic(-1001234567890, 88).project_id, "example-project"
        )


if __name__ == "__main__":
    unittest.main()
