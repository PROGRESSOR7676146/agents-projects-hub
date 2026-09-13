from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_codex_router.session_adoption_state import AdoptionRequest, CodexSessionOrigins
from hermes_codex_router.state import HubState, StateError


class SessionAdoptionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "state.db"
        self.state = HubState.open(self.path)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example", chat_id=-1001, thread_id=7, title="Example"
        )
        self.origins = CodexSessionOrigins(self.state)
        self.request = AdoptionRequest(
            "example", -1001, 7, "example-thread", self.root, "example-model", "high"
        )

    def attach(self, request=None):
        request = request or self.request
        target = self.origins.preview(request)
        return self.origins.attach(
            request, expected_session_id=target.session.session_id if target.session else None
        )

    def activate(self, session, message_id=50):
        return self.state.return_codex_local_writer(
            chat_id=-1001,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            session_id=session.session_id,
            observer_agent_id="hub",
        )

    def enqueue(self, session, message_id=51):
        return self.state.enqueue_provider_job(
            idempotency_key=f"example:{message_id}",
            chat_id=-1001,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=session.provider_session_id,
            model=session.model,
            effort=session.effort,
            payload_text="Example task",
        )

    def test_empty_attach_is_atomic_local_and_repeat_after_return_is_noop(self) -> None:
        result = self.attach()
        session = result.session
        self.assertIsNotNone(session)
        self.assertEqual(session.writer_mode, "local")
        self.assertEqual(session.provider_session_id, "example-thread")
        self.assertEqual(self.origins.require(session.session_id).canonical_root, self.root)
        with self.assertRaises(StateError):
            self.enqueue(session)
        self.activate(session)
        repeated = self.attach()
        self.assertTrue(repeated.already_attached)
        self.assertEqual(repeated.session.session_id, session.session_id)
        self.assertEqual(repeated.session.writer_mode, "telegram")
        self.assertEqual(self.origins.require(session.session_id).activation_message_id, 50)
        self.enqueue(repeated.session)

    def test_placeholder_gets_new_identity_and_stale_admission_is_rejected(self) -> None:
        previous = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        session = self.attach().session
        self.assertNotEqual(session.session_id, previous.session_id)
        self.assertEqual(session.generation, previous.generation + 1)
        self.assertEqual(self.state.get_session(previous.session_id).status, "archived")
        self.activate(session)
        with self.assertRaises(StateError):
            self.enqueue(previous)

    def test_replace_preserves_history_and_idle_satellite(self) -> None:
        from dataclasses import replace

        previous = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        self.state.bind_provider_session(previous.session_id, "previous-thread", None)
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "opencode", "other-model", "high"
        )
        self.state.record_forwarded_quote(
            topic_id=self.topic.topic_id,
            chat_id=-1001,
            message_id=20,
            observer_agent_id="hub",
            text="Previous quotation",
        )
        request = replace(self.request, replaces_session_id=previous.session_id)
        session = self.attach(request).session
        self.assertEqual(self.state.get_session(previous.session_id).status, "archived")
        self.assertEqual(self.state.get_session(satellite.session_id), satellite)
        self.assertEqual(
            self.origins.require(session.session_id).replaces_session_id, previous.session_id
        )
        self.activate(session)
        self.assertEqual(self.attach(request).session.session_id, session.session_id)
        self.assertIsNotNone(self.state.visible_context_snapshot(self.topic.topic_id, "codex"))
        self.assertEqual(
            self.state.unseen_forwarded_context(self.topic.topic_id, "codex"), (None, None)
        )

    def test_delayed_inputs_and_quotes_do_not_cross_first_return_boundary(self) -> None:
        session = self.attach().session
        self.activate(session)
        for message_id in (1, 49, 50):
            with (
                self.subTest(message_id=message_id),
                self.assertRaisesRegex(StateError, "activation"),
            ):
                self.enqueue(session, message_id)
        for message_id, text in [(25, "Old delayed quotation"), (52, "New quotation")]:
            self.state.record_forwarded_quote(
                topic_id=self.topic.topic_id,
                chat_id=-1001,
                message_id=message_id,
                observer_agent_id="hub",
                text=text,
            )
        forwarded, _ = self.state.unseen_forwarded_context(self.topic.topic_id, "codex")
        assert forwarded is not None
        self.assertIn("New quotation", forwarded)
        self.assertNotIn("Old delayed", forwarded)
        other, _ = self.state.unseen_forwarded_context(self.topic.topic_id, "opencode")
        assert other is not None
        self.assertIn("Old delayed", other)
        self.state.set_writer_mode(session.session_id, "local")
        self.activate(session, 60)
        self.assertEqual(self.origins.require(session.session_id).activation_message_id, 50)

    def test_busy_and_unresolved_work_block_replace(self) -> None:
        from dataclasses import replace

        previous = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        self.state.bind_provider_session(previous.session_id, "previous-thread", None)
        request = replace(self.request, replaces_session_id=previous.session_id)
        job, _ = self.enqueue(self.state.get_session(previous.session_id))
        for status in (
            "queued",
            "leased",
            "executing",
            "retry_wait",
            "result_ready",
            "indeterminate",
        ):
            with self.subTest(status=status):
                with self.state._connection:
                    lease = "example-lease" if status in ("leased", "executing") else None
                    self.state._connection.execute(
                        "UPDATE provider_jobs SET status = ?, lease_owner=?, lease_token=?, lease_expires_at=? WHERE job_id = ?",
                        (status, lease, lease, lease, job.job_id),
                    )
                with self.assertRaises(StateError):
                    self.attach(request)
                current = self.state.active_session(self.topic.topic_id)
                assert current is not None
                self.assertEqual(current.session_id, previous.session_id)

    def test_transaction_faults_preserve_original_binding(self) -> None:
        previous = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        for table, operation in (
            ("agent_sessions", "INSERT"),
            ("codex_session_origins", "INSERT"),
            ("topics", "UPDATE"),
        ):
            with self.subTest(table=table):
                self.state._connection.execute(
                    f"CREATE TEMP TRIGGER fail_attach BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'example fault'); END"
                )
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.attach()
                    self.assertEqual(self.state.active_session(self.topic.topic_id), previous)
                    self.assertEqual(
                        self.state._connection.execute(
                            "SELECT COUNT(*) FROM codex_session_origins"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    self.state._connection.execute("DROP TRIGGER fail_attach")

    def test_repeat_after_new_does_not_resurrect_binding(self) -> None:
        session = self.attach().session
        self.activate(session)
        self.state.new_active_session(self.topic.topic_id)
        with self.assertRaisesRegex(StateError, "superseded"):
            self.attach()
        self.assertIsNotNone(self.origins.get(session.session_id))

    def test_concurrent_attach_has_one_winner_and_no_partial_binding(self) -> None:
        from dataclasses import replace

        barrier = threading.Barrier(2)

        def attempt(thread_id):
            state = HubState.open(self.path)
            try:
                origins = CodexSessionOrigins(state)
                request = replace(self.request, provider_thread_id=thread_id)
                target = origins.preview(request)
                barrier.wait(timeout=5)
                try:
                    return origins.attach(
                        request,
                        expected_session_id=target.session.session_id if target.session else None,
                    ).session.session_id
                except StateError:
                    return None
            finally:
                state.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("first-thread", "second-thread")))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(
            self.state._connection.execute("SELECT COUNT(*) FROM codex_session_origins").fetchone()[
                0
            ],
            1,
        )

    def test_source_reservation_and_root_local_writer_conflicts(self) -> None:
        from dataclasses import replace

        session = self.attach().session
        other_topic = self.state.observe_topic(
            project_id="example", chat_id=-1001, thread_id=8, title="Other"
        )
        with self.assertRaises(StateError):
            self.attach(replace(self.request, thread_id=8))
        with self.assertRaises(StateError):
            self.attach(replace(self.request, thread_id=8, provider_thread_id="other-thread"))
        self.assertIsNone(self.state.active_session(other_topic.topic_id))
        self.assertEqual(self.state.get_session(session.session_id).writer_mode, "local")

    def test_stale_preview_rejects_new_generation(self) -> None:
        previous = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        self.origins.preview(self.request)
        current = self.state.new_active_session(self.topic.topic_id)
        with self.assertRaisesRegex(StateError, "target_changed"):
            self.origins.attach(self.request, expected_session_id=previous.session_id)
        self.assertEqual(self.state.active_session(self.topic.topic_id), current)

    def test_first_return_rolls_back_writer_floor_and_receipt_together(self) -> None:
        session = self.attach().session
        self.state._connection.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON observed_messages BEGIN SELECT RAISE(ABORT, 'fictional fault'); END;"
        )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.activate(session)
            self.assertEqual(self.state.get_session(session.session_id).writer_mode, "local")
            self.assertIsNone(self.origins.require(session.session_id).activation_message_id)
            self.assertEqual(
                self.state._connection.execute("SELECT COUNT(*) FROM observed_messages").fetchone()[
                    0
                ],
                0,
            )
        finally:
            self.state._connection.execute("DROP TRIGGER fail_receipt")
        self.activate(session)

    def test_control_compare_and_swap_cannot_mutate_attached_generation(self) -> None:
        old = self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        session = self.attach().session
        self.activate(session)
        for operation in (
            lambda: self.state.new_active_session(
                self.topic.topic_id, expected_session_id=old.session_id
            ),
            lambda: self.state.replace_active_session(
                self.topic.topic_id,
                model="changed",
                effort="low",
                expected_session_id=old.session_id,
            ),
            lambda: self.state.activate_agent(
                self.topic.topic_id, "opencode", "model", "high", expected_session_id=old.session_id
            ),
        ):
            with self.assertRaisesRegex(StateError, "changed"):
                operation()
        self.assertEqual(
            self.state.active_session(self.topic.topic_id),
            self.state.get_session(session.session_id),
        )

    def test_provider_switch_and_model_change_keep_origin_until_explicit_new(self) -> None:
        session = self.attach().session
        self.activate(session)
        self.state.activate_agent(self.topic.topic_id, "opencode", "model", "high")
        self.state.activate_agent(self.topic.topic_id, "codex", "example-model", "high")
        selected = self.state.replace_active_session(
            self.topic.topic_id, model="other-model", effort="medium"
        )
        self.assertEqual(selected.session_id, session.session_id)
        self.assertEqual(selected.provider_session_id, session.provider_session_id)
        new = self.state.new_active_session(self.topic.topic_id)
        self.assertIsNone(new.provider_session_id)
        self.assertIsNone(self.origins.get(new.session_id))
        self.assertIsNotNone(self.origins.get(session.session_id))

    def test_model_control_rechecks_queue_admission_inside_its_transaction(self) -> None:
        session = self.attach().session
        self.activate(session)
        self.enqueue(self.state.get_session(session.session_id))
        with self.assertRaisesRegex(StateError, "pending"):
            self.state.replace_active_session(
                self.topic.topic_id,
                model="other-model",
                effort="low",
                expected_session_id=session.session_id,
            )
        self.assertEqual(self.state.get_session(session.session_id).model, "example-model")

    def test_known_same_root_checkpoint_blocks_old_registration_writer(self) -> None:
        old_topic = self.state.observe_topic(
            project_id="previous-registration", chat_id=-1002, thread_id=9, title="Previous"
        )
        old = self.state.activate_agent(old_topic.topic_id, "codex", "example-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="previous-registration",
            chat_id=-1002,
            message_id=1,
            topic_id=old_topic.topic_id,
            agent_id="codex",
            session_id=old.session_id,
            session_generation=old.generation,
            provider_session_id=None,
            model=old.model,
            effort=old.effort,
            payload_text="Previous work",
        )
        with self.state._connection:
            self.state._connection.execute(
                "INSERT INTO provider_execution_checkpoints (job_id,provider_thread_id,project_root,updated_at) VALUES (?,?,?,?)",
                (job.job_id, "previous-thread", str(self.root), "now"),
            )
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='completed' WHERE job_id=?", (job.job_id,)
            )
        self.state.set_writer_mode(old.session_id, "local")
        with self.assertRaisesRegex(StateError, "local_writer"):
            self.attach()
