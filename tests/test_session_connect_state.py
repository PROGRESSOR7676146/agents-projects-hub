from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import TypeVar

from hermes_codex_router.session_connect import ConnectCandidate, SessionConnectStore
from hermes_codex_router.state import HubState, StateError

T = TypeVar("T")


def required(value: T | None) -> T:
    assert value is not None
    return value


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
        leased = required(self.store.lease_worker("worker-1"))
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
        leased = required(self.store.lease_worker("worker-1"))
        self.store.prepare_marker(leased.workflow_id, leased.lease_token)
        outbox = required(self.store.lease_outbox("sender-1"))
        self.assertEqual(outbox.kind, "activation_marker")
        completed = self.store.complete_marker(outbox, telegram_message_id=120)
        self.assertEqual(completed.stage, "completed")
        session = self.state.get_session(required(completed.result_session_id))
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

    def test_expired_discovery_sends_one_durable_restart_notice(self) -> None:
        workflow = self.store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
            model="gpt-5.6-sol",
            effort="high",
        )
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE session_connect_workflows SET expires_at=? WHERE workflow_id=?",
                ("2000-01-01T00:00:00+00:00", workflow.workflow_id),
            )
        self.assertIsNone(self.store.lease_worker("worker-before-restart"))
        restarted_state = HubState.open(self.root / "state.db")
        try:
            restarted = SessionConnectStore(restarted_state)
            self.assertIsNone(restarted.lease_worker("worker-after-restart"))
            self.assertEqual(restarted.get(workflow.workflow_id).stage, "expired")
            notices = restarted_state._connection.execute(
                "SELECT kind,telegram_html FROM session_connect_outbox WHERE workflow_id=?",
                (workflow.workflow_id,),
            ).fetchall()
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0][0], "notice")
            self.assertIn("/connect", notices[0][1])
        finally:
            restarted_state.close()

    def test_expiry_during_metadata_read_cannot_be_reclassified_as_failure(self) -> None:
        workflow = self.store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
            model="gpt-5.6-sol",
            effort="high",
        )
        leased = required(self.store.lease_worker("worker-1"))
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE session_connect_workflows SET expires_at=? WHERE workflow_id=?",
                ("2000-01-01T00:00:00+00:00", workflow.workflow_id),
            )
        self.assertIsNone(self.store.lease_worker("worker-2"))
        with self.assertRaisesRegex(StateError, "connect_worker_lease_changed"):
            self.store.finish_discovery(workflow.workflow_id, leased.lease_token, ())
        with self.assertRaisesRegex(StateError, "connect_worker_lease_changed"):
            self.store.fail_worker(workflow.workflow_id, leased.lease_token, "metadata_unavailable")
        self.assertEqual(self.store.get(workflow.workflow_id).stage, "expired")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM session_connect_outbox WHERE workflow_id=?",
                (workflow.workflow_id,),
            ).fetchone()[0],
            1,
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
        leased = required(self.store.lease_worker("worker-1"))
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-2", "new-thread", "Сессия · thread", 1),),
        )
        selected = self.store.select_candidate(42, candidates[0].candidate_id)
        self.store.request_activation(42, selected.workflow_id)
        leased = required(self.store.lease_worker("worker-1"))
        self.store.prepare_marker(leased.workflow_id, leased.lease_token)
        outbox = required(self.store.lease_outbox("sender-1"))
        self.store.mark_marker_unknown(outbox, "telegram_outcome_unknown")
        self.assertEqual(
            required(self.state.active_session(self.topic.topic_id)).session_id,
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

        leased = required(self.store.lease_worker("worker-1"))
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
        leased = required(self.store.lease_worker("worker-1"))
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
            required(self.state.find_topic(-1001234567890, 88)).project_id,
            "example-project",
        )

    def test_one_time_code_claim_is_idempotent_and_consumed_only_on_activation(self) -> None:
        issued = self.store.issue_code(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            source=ConnectCandidate("candidate-5", "saved-thread", "Сессия · saved", 10),
            model="gpt-5.6-sol",
            effort="high",
        )
        first = self.store.redeem_code_topic(
            owner_user_id=42,
            code=issued.code,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
        )
        repeated = self.store.redeem_code_topic(
            owner_user_id=42,
            code=issued.code,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
        )
        first_workflow = required(first.workflow)
        repeated_workflow = required(repeated.workflow)
        self.assertEqual(first_workflow.workflow_id, repeated_workflow.workflow_id)
        self.assertFalse(first.already_consumed)
        self.assertIsNone(
            self.state._connection.execute(
                "SELECT consumed_at FROM session_connect_codes WHERE code_id=?",
                (issued.code_id,),
            ).fetchone()[0]
        )
        self.store.request_activation(42, first_workflow.workflow_id)
        leased = required(self.store.lease_worker("worker-1"))
        self.store.prepare_marker(leased.workflow_id, leased.lease_token)
        outbox = required(self.store.lease_outbox("sender-1"))
        completed = self.store.complete_marker(outbox, telegram_message_id=121)
        after = self.store.redeem_code_topic(
            owner_user_id=42,
            code=issued.code,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
        )
        self.assertTrue(after.already_consumed)
        self.assertEqual(after.result_session_id, completed.result_session_id)

    def test_code_failures_are_owner_scoped_and_rate_limited(self) -> None:
        issued = self.store.issue_code(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            source=ConnectCandidate("candidate-6", "saved-thread", "Сессия · saved", 10),
            model="gpt-5.6-sol",
            effort="high",
        )
        for _ in range(5):
            with self.assertRaisesRegex(StateError, "connect_code_invalid"):
                self.store.redeem_code_direct(owner_user_id=42, code="WRONGCODE")
        with self.assertRaisesRegex(StateError, "connect_code_rate_limited"):
            self.store.redeem_code_direct(owner_user_id=42, code=issued.code)

    def test_expired_code_is_rejected_without_creating_a_workflow(self) -> None:
        issued = self.store.issue_code(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            source=ConnectCandidate("candidate-7", "saved-thread", "Сессия · saved", 10),
            model="gpt-5.6-sol",
            effort="high",
        )
        self.state._connection.execute(
            "UPDATE session_connect_codes SET expires_at='2000-01-01T00:00:00+00:00' WHERE code_id=?",
            (issued.code_id,),
        )
        self.state._connection.commit()
        with self.assertRaisesRegex(StateError, "connect_code_invalid"):
            self.store.redeem_code_direct(owner_user_id=42, code=issued.code)

    def test_concurrent_code_redemption_returns_one_claimed_workflow(self) -> None:
        issued = self.store.issue_code(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            source=ConnectCandidate("candidate-8", "saved-thread", "Сессия · saved", 10),
            model="gpt-5.6-sol",
            effort="high",
        )
        barrier = threading.Barrier(2)
        results: list[str] = []
        failures: list[BaseException] = []

        def redeem() -> None:
            state = HubState.open(self.root / "state.db")
            try:
                barrier.wait()
                result = SessionConnectStore(state).redeem_code_direct(
                    owner_user_id=42, code=issued.code
                )
                assert result.workflow is not None
                results.append(result.workflow.workflow_id)
            except BaseException as exc:
                failures.append(exc)
            finally:
                state.close()

        threads = [threading.Thread(target=redeem) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1)

    def test_stale_selection_and_changed_target_never_replace_current_session(self) -> None:
        workflow = self.store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
            model="gpt-5.6-sol",
            effort="high",
        )
        leased = required(self.store.lease_worker("worker-1"))
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-9", "saved-thread", "Сессия · saved", 10),),
        )
        self.store.select_candidate(42, candidates[0].candidate_id)
        with self.assertRaisesRegex(StateError, "connect_selection_stale"):
            self.store.select_candidate(42, candidates[0].candidate_id)
        with self.assertRaisesRegex(StateError, "connect_selection_stale"):
            self.store.select_candidate(99, candidates[0].candidate_id)

        changed = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        with self.assertRaisesRegex(StateError, "target_changed"):
            self.store.request_activation(42, workflow.workflow_id)
        self.assertEqual(self.state.active_session(self.topic.topic_id), changed)
        self.assertEqual(
            self.state._connection.execute("SELECT COUNT(*) FROM codex_session_origins").fetchone()[
                0
            ],
            0,
        )

    def test_work_appearing_after_confirmation_blocks_marker_without_partial_archive(self) -> None:
        current = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        current = self.state.bind_provider_session(current.session_id, "old-thread", None)
        workflow = self.store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=-1001234567890,
            thread_id=77,
            model="gpt-5.6-sol",
            effort="high",
        )
        leased = required(self.store.lease_worker("worker-1"))
        candidates = self.store.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-10", "saved-thread", "Сессия · saved", 10),),
        )
        selected = self.store.select_candidate(42, candidates[0].candidate_id)
        self.store.request_activation(42, selected.workflow_id)
        self.state.enqueue_provider_job(
            idempotency_key="new-work",
            chat_id=-1001234567890,
            message_id=122,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=current.session_id,
            session_generation=current.generation,
            provider_session_id=current.provider_session_id,
            model=current.model,
            effort=current.effort,
            payload_text="New work",
        )
        leased = required(self.store.lease_worker("worker-1"))
        with self.assertRaises(StateError):
            self.store.prepare_marker(leased.workflow_id, leased.lease_token)
        self.assertEqual(
            required(self.state.active_session(self.topic.topic_id)).session_id,
            current.session_id,
        )
        self.assertEqual(
            self.state._connection.execute("SELECT COUNT(*) FROM codex_session_origins").fetchone()[
                0
            ],
            0,
        )

    def test_workflow_survives_restart_and_unknown_topic_creation_is_not_retried(self) -> None:
        workflow = self.store.start_direct(
            owner_user_id=42,
            projects=(("example-project", self.root, "Example project"),),
            model="gpt-5.6-sol",
            effort="high",
        )
        option = self.store.options(workflow.workflow_id, "project")[0]
        self.store.select_project(42, option.option_id)
        reopened = HubState.open(self.root / "state.db")
        self.addCleanup(reopened.close)
        restarted = SessionConnectStore(reopened)
        self.assertEqual(restarted.get(workflow.workflow_id).stage, "discovering")

        leased = required(restarted.lease_worker("worker-after-restart"))
        candidates = restarted.finish_discovery(
            workflow.workflow_id,
            leased.lease_token,
            (ConnectCandidate("candidate-11", "saved-thread", "Сессия · saved", 10),),
        )
        restarted.select_candidate(42, candidates[0].candidate_id)
        restarted.request_new_topic(42, workflow.workflow_id)
        restarted.begin_topic_creation(42, workflow.workflow_id)
        unknown = restarted.topic_creation_unknown(42, workflow.workflow_id)
        self.assertEqual(unknown.stage, "topic_create_unknown")
        with self.assertRaisesRegex(StateError, "connect_topic_creation_stale"):
            restarted.begin_topic_creation(42, workflow.workflow_id)


if __name__ == "__main__":
    unittest.main()
