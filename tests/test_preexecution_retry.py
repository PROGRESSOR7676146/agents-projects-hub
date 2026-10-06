from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_failure import (
    CodexPreparationError,
    CodexRetryBindingError,
    UnsupportedCodexPermissionProfileError,
)
from hermes_codex_router.codex_retry_policy import (
    PreparationRetryBinding,
    preparation_retry_binding,
)
from hermes_codex_router.codex_rpc import RpcError, RpcRejectedError
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.incoming_materials import IncomingMaterialDraft
from hermes_codex_router.preexecution_retry_state import (
    PreexecutionRetryState,
    PreparationRetryRefused,
)
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.worker_execution import (
    codex_turn_text,
    require_exact_retry_transport,
    validate_codex_worker_binding,
)
from tests import test_codex_worker as worker_fixtures
from tests.hub_service_harness import CHAT_ID, CODEX, THREAD_ID, HubHarness, text_update


class PreexecutionRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.harness = HubHarness(Path(temp.name))
        self.addCleanup(self.harness.close)
        self.state = self.harness.service.state
        self.session = self.harness.activate(CODEX, provider_session_id=None)
        self.topic = self.harness.topic()
        self.payload = "Run the six approved fictional scenarios: A, B, C, D, E, F."
        self.job, _ = self.state.enqueue_provider_job(
            idempotency_key="example:matrix",
            chat_id=CHAT_ID,
            message_id=20,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text=self.payload,
        )

    def preparation_failure(
        self, job_id: str, notice_id: int, *, prepared_thread: str | None = None
    ) -> None:
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.assertEqual(lease.job_id, job_id)
        self.state.mark_provider_job_executing(job_id, lease.lease_token)
        if prepared_thread is not None:
            ExecutionJournal(self.state).record_thread(
                job_id, lease.lease_token, prepared_thread, self.harness.root
            )
        self.state.terminate_provider_job_with_notice(
            job_id,
            lease.lease_token,
            status="failed",
            error_class="pre_execution",
            error_code=CodexPreparationError.__name__,
            sender_agent_id="codex",
            telegram_html="Example preparation failed",
            preparation_retry=PreparationRetryBinding(self.harness.root, None),
        )
        delivery = self.state.lease_telegram_outbox("codex", "example-sender")
        assert delivery is not None and delivery.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            delivery.outbox_id,
            delivery.lease_token,
            telegram_message_id=notice_id,
        )

    def retry(self, source: str, notice_id: int, reply_id: int = 30):
        return PreexecutionRetryState(self.state).retry_from_notice(
            source_job_id=source,
            chat_id=CHAT_ID,
            thread_id=THREAD_ID,
            notice_message_id=notice_id,
            reply_message_id=reply_id,
            canonical_root=self.harness.root,
            model_provider=None,
        )

    def test_exact_saved_task_survives_two_preparation_failures(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        child, created = self.retry(self.job.job_id, 101)
        self.assertTrue(created)
        self.assertEqual(child.payload_text, self.payload)
        self.assertEqual(
            tuple(
                row[0]
                for row in self.sql(
                    "SELECT input_text FROM provider_job_inputs WHERE job_id=?", (child.job_id,)
                )
            ),
            ("retry",),
        )
        self.preparation_failure(child.job_id, 102)
        next_child, created = self.retry(child.job_id, 102, 31)
        self.assertTrue(created)
        self.assertEqual(next_child.payload_text, self.payload)
        self.assertEqual(self.state.get_provider_job(self.job.job_id).payload_text, self.payload)
        self.assertIsNone(self.sql("SELECT 1 FROM provider_job_continuations LIMIT 1").fetchone())

    def test_restart_and_distinct_replies_do_not_create_another_child(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        child, _ = self.retry(self.job.job_id, 101)
        reopened = HubState.open(self.harness.config.state_path, codex_permission_profile=None)
        self.addCleanup(reopened.close)
        repeated, created = PreexecutionRetryState(reopened).retry_from_notice(
            source_job_id=self.job.job_id,
            chat_id=CHAT_ID,
            thread_id=THREAD_ID,
            notice_message_id=101,
            reply_message_id=31,
            canonical_root=self.harness.root,
            model_provider=None,
        )
        self.assertFalse(created)
        self.assertEqual(repeated.job_id, child.job_id)
        self.assertTrue(reopened.message_already_observed(CHAT_ID, 31))
        self.assertEqual(len(reopened.provider_jobs_for_topic(self.topic.topic_id)), 2)

    def test_ticket_and_failure_outbox_roll_back_together(self) -> None:
        self.sql(
            "CREATE TRIGGER example_outbox_fault BEFORE INSERT ON telegram_outbox "
            "BEGIN SELECT RAISE(ABORT,'example fault'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.preparation_failure(self.job.job_id, 101)
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")
        self.assertIsNone(
            self.sql("SELECT 1 FROM provider_preexecution_retry_tickets LIMIT 1").fetchone()
        )

    def test_source_after_record_thread_uses_prepared_thread_without_changing_snapshot(
        self,
    ) -> None:
        original = ExecutionJournal.record_thread

        def bind_then_fail(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            raise RpcError("Codex notification buffer exceeded its bound")

        # Use the real embedded worker and Controller retry boundary.
        with patch.object(ExecutionJournal, "record_thread", bind_then_fail):
            self.assertTrue(self.harness.service.run_embedded_queue_cycle())
        old = self.state.get_provider_job(self.job.job_id)
        self.assertEqual(old.status, "failed")
        self.assertIsNone(old.provider_session_id)
        self.assertEqual(self.harness.session().provider_session_id, "thread-1")
        notice = self.state.get_telegram_outbox_for_job(old.job_id)
        assert notice.telegram_message_id is not None
        update = text_update(30, "retry")
        cast(dict[str, Any], update["message"])["reply_to_message"] = {
            "message_id": notice.telegram_message_id,
        }
        self.assertTrue(self.harness.service.handle_update(update))
        child = self.state.provider_jobs_for_topic(self.topic.topic_id)[-1]
        self.assertEqual(child.provider_session_id, "thread-1")
        self.assertEqual(child.payload_text, self.payload)
        self.assertFalse(self.harness.service.handle_update(update))
        self.assertTrue(self.harness.service.run_embedded_queue_cycle())
        self.assertEqual(self.harness.client.turns, 1)
        self.assertEqual(self.state.get_provider_job(child.job_id).status, "completed")

    def test_failed_child_admission_rolls_back_every_effect_then_redelivers(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        for table in (
            "provider_preexecution_retries",
            "provider_preexecution_retry_controls",
            "provider_job_inputs",
            "task_lifecycle_notices",
        ):
            with self.subTest(table=table):
                before = self.snapshot()
                self.sql(
                    f"CREATE TRIGGER example_retry_fault BEFORE INSERT ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'example fault'); END"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    self.retry(self.job.job_id, 101)
                self.assertEqual(self.snapshot(), before)
                self.sql("DROP TRIGGER example_retry_fault")
        child, created = self.retry(self.job.job_id, 101)
        self.assertTrue(created)
        self.assertEqual(child.payload_text, self.payload)

    def test_existing_thread_preparation_can_retry_only_when_identity_is_retained(self) -> None:
        self.state.bind_provider_session(self.session.session_id, "example-original-thread", None)
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            ("example-original-thread", self.job.job_id),
        )
        self.preparation_failure(self.job.job_id, 101, prepared_thread="example-original-thread")
        child, created = self.retry(self.job.job_id, 101)
        self.assertTrue(created)
        self.assertEqual(child.provider_session_id, "example-original-thread")
        self.assertEqual(child.payload_text, self.payload)

    def test_replaced_existing_thread_has_visible_refusal_without_retry_ticket(self) -> None:
        self.state.bind_provider_session(self.session.session_id, "example-original-thread", None)
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            ("example-original-thread", self.job.job_id),
        )
        self.preparation_failure(self.job.job_id, 101, prepared_thread="example-replacement-thread")
        notice = self.state.get_telegram_outbox_for_job(self.job.job_id)
        self.assertNotIn("Reply exactly retry", notice.telegram_html)
        self.assertIn("no saved context snapshot", notice.telegram_html)
        self.assertIsNone(
            self.sql("SELECT 1 FROM provider_preexecution_retry_tickets LIMIT 1").fetchone()
        )
        old = self.state.get_provider_job(self.job.job_id)
        self.assertEqual(old.provider_session_id, "example-original-thread")
        self.assertEqual(old.payload_text, self.payload)
        checkpoint = self.sql(
            "SELECT provider_thread_id FROM provider_execution_checkpoints WHERE job_id=?",
            (old.job_id,),
        ).fetchone()
        assert checkpoint is not None
        self.assertEqual(checkpoint[0], "example-replacement-thread")

    def test_previously_saved_replacement_thread_ticket_refuses_admission_and_execution(
        self,
    ) -> None:
        # Represent a ticket persisted by the previous schema-41 candidate.
        self.preparation_failure(self.job.job_id, 101, prepared_thread="example-replacement-thread")
        child, _ = self.retry(self.job.job_id, 101)
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            ("example-original-thread", self.job.job_id),
        )
        before = self.snapshot()
        with self.assertRaisesRegex(PreparationRetryRefused, "no saved context snapshot"):
            PreexecutionRetryState(self.state)._validate(
                self.job.job_id, canonical_root=self.harness.root, model_provider=None
            )
        self.assertEqual(self.snapshot(), before)
        with (
            patch.object(self.harness.service, "_client", side_effect=AssertionError("no client")),
            patch(
                "hermes_codex_router.service.prepare_worker_materials",
                side_effect=AssertionError("no materials"),
            ),
            patch(
                "hermes_codex_router.service.prepare_worker_staging_directory",
                side_effect=AssertionError("no staging"),
            ),
        ):
            self.assertTrue(self.harness.service.run_embedded_queue_cycle())
        failed = self.state.get_provider_job(child.job_id)
        self.assertEqual(failed.error_code, "CodexRetryBindingError")
        self.assertEqual(self.harness.client.turns, 0)
        self.assertIsNone(
            self.sql(
                "SELECT 1 FROM provider_preexecution_retry_tickets WHERE source_job_id=?",
                (child.job_id,),
            ).fetchone()
        )

    def test_legacy_replacement_ancestor_refuses_new_and_already_queued_descendants(self) -> None:
        self.preparation_failure(self.job.job_id, 101, prepared_thread="example-replacement-thread")
        child, _ = self.retry(self.job.job_id, 101)
        self.preparation_failure(child.job_id, 102, prepared_thread="example-replacement-thread")
        # The old candidate allowed A→B, then B→B. Represent that ancestry.
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            ("example-original-thread", self.job.job_id),
        )
        before = self.snapshot()
        with self.assertRaisesRegex(PreparationRetryRefused, "no saved context snapshot"):
            self.retry(child.job_id, 102, 31)
        self.assertEqual(self.snapshot(), before)
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=NULL WHERE job_id=?", (self.job.job_id,)
        )
        grandchild, _ = self.retry(child.job_id, 102, 31)
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            ("example-original-thread", self.job.job_id),
        )
        with (
            patch.object(self.harness.service, "_client", side_effect=AssertionError("no client")),
            patch(
                "hermes_codex_router.service.prepare_worker_materials",
                side_effect=AssertionError("no materials"),
            ),
            patch(
                "hermes_codex_router.service.prepare_worker_staging_directory",
                side_effect=AssertionError("no staging"),
            ),
        ):
            self.assertTrue(self.harness.service.run_embedded_queue_cycle())
        self.assertEqual(
            self.state.get_provider_job(grandchild.job_id).error_code, "CodexRetryBindingError"
        )
        self.assertEqual(self.harness.client.turns, 0)

    def test_legacy_replacement_ancestor_prevents_a_fresh_descendant_ticket(self) -> None:
        self.preparation_failure(self.job.job_id, 101, prepared_thread="example-replacement-thread")
        child, _ = self.retry(self.job.job_id, 101)
        self.sql(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            ("example-original-thread", self.job.job_id),
        )
        self.preparation_failure(child.job_id, 102, prepared_thread="example-replacement-thread")
        self.assertIn(
            "no saved context snapshot",
            self.state.get_telegram_outbox_for_job(child.job_id).telegram_html,
        )
        self.assertIsNone(
            self.sql(
                "SELECT 1 FROM provider_preexecution_retry_tickets WHERE source_job_id=?",
                (child.job_id,),
            ).fetchone()
        )

    def test_cyclic_retry_ancestry_is_a_bounded_visible_refusal(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        self.retry(self.job.job_id, 101)
        self.sql(
            "UPDATE provider_preexecution_retries SET child_job_id=? WHERE source_job_id=?",
            (self.job.job_id, self.job.job_id),
        )
        with self.assertRaisesRegex(PreparationRetryRefused, "retry ancestry"):
            PreexecutionRetryState(self.state)._validate(
                self.job.job_id, canonical_root=self.harness.root, model_provider=None
            )

    def sql(self, statement, parameters=()):
        with self.state._connection:
            return self.state._connection.execute(statement, parameters)

    def snapshot(self):
        return {
            table: [tuple(row) for row in self.sql(f"SELECT * FROM {table}")]
            for table in (
                "provider_jobs",
                "provider_job_inputs",
                "topic_queue_counters",
                "provider_preexecution_retries",
                "provider_preexecution_retry_controls",
                "observed_messages",
                "task_lifecycle_notices",
            )
        }

    def test_notice_chat_topic_delivery_and_sender_are_exact(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        retry = PreexecutionRetryState(self.state)
        for chat, thread, notice in (
            (CHAT_ID - 1, THREAD_ID, 101),
            (CHAT_ID, THREAD_ID + 1, 101),
            (CHAT_ID, THREAD_ID, 999),
        ):
            self.assertIsNone(
                retry.source_for_notice(chat_id=chat, thread_id=thread, notice_message_id=notice)
            )
        self.sql("UPDATE telegram_outbox SET sender_agent_id='other'")
        self.assertIsNone(
            retry.source_for_notice(chat_id=CHAT_ID, thread_id=THREAD_ID, notice_message_id=101)
        )
        self.assertFalse(self.state.message_already_observed(CHAT_ID, 30))

    def test_null_thread_and_other_binding_drift_refuse_at_admission_and_execution(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        child, _ = self.retry(self.job.job_id, 101)
        for column, value in (
            ("provider_session_id", "example-other-thread"),
            ("model", "example-other-model"),
            ("effort", "low"),
            ("generation", self.session.generation + 1),
            ("writer_mode", "local"),
        ):
            with self.subTest(column=column):
                old = self.sql(
                    f"SELECT {column} FROM agent_sessions WHERE session_id=?",
                    (self.session.session_id,),
                ).fetchone()[0]
                self.sql(
                    f"UPDATE agent_sessions SET {column}=? WHERE session_id=?",
                    (value, self.session.session_id),
                )
                with self.assertRaises(PreparationRetryRefused):
                    PreexecutionRetryState(self.state)._validate(
                        self.job.job_id,
                        canonical_root=self.harness.root,
                        model_provider=None,
                    )
                with self.assertRaises(CodexRetryBindingError):
                    validate_codex_worker_binding(
                        self.state, child, self.harness.service.config, self.harness.root
                    )
                self.sql(
                    f"UPDATE agent_sessions SET {column}=? WHERE session_id=?",
                    (old, self.session.session_id),
                )
        with self.assertRaises(CodexRetryBindingError):
            validate_codex_worker_binding(
                self.state,
                child,
                replace(self.harness.service.config, codex_model_provider="example-route"),
                self.harness.root,
            )
        with self.assertRaises(CodexRetryBindingError):
            validate_codex_worker_binding(
                self.state, child, self.harness.service.config, self.harness.root.parent
            )
        self.assertEqual(self.harness.client.turns, 0)

    def test_changed_queued_binding_fails_before_materials_staging_or_client(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        child, _ = self.retry(self.job.job_id, 101)
        self.state.bind_provider_session(self.session.session_id, "example-other-thread", None)
        with (
            patch.object(self.harness.service, "_client", side_effect=AssertionError("no client")),
            patch(
                "hermes_codex_router.service.prepare_worker_materials",
                side_effect=AssertionError("no materials"),
            ),
            patch(
                "hermes_codex_router.service.prepare_worker_staging_directory",
                side_effect=AssertionError("no staging"),
            ),
        ):
            self.assertTrue(self.harness.service.run_embedded_queue_cycle())
        failed = self.state.get_provider_job(child.job_id)
        self.assertEqual(failed.error_code, "CodexRetryBindingError")
        self.assertIsNone(
            self.sql(
                "SELECT 1 FROM provider_preexecution_retry_tickets WHERE source_job_id=?",
                (child.job_id,),
            ).fetchone()
        )

    def test_child_commit_fault_keeps_reply_recoverable(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        connection = self.state._connection

        class CommitFault:
            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                raise sqlite3.OperationalError("example commit fault")

        before = self.snapshot()
        with patch.object(self.state, "_connection", cast(Any, CommitFault())):
            with self.assertRaises(sqlite3.OperationalError):
                self.retry(self.job.job_id, 101)
        self.assertEqual(self.snapshot(), before)
        child, created = self.retry(self.job.job_id, 101)
        self.assertTrue(created)
        self.assertEqual(child.payload_text, self.payload)

    def test_failure_ticket_commit_fault_leaves_all_failure_effects_uncommitted(self) -> None:
        connection = self.state._connection
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(self.job.job_id, lease.lease_token)

        class CommitFault:
            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                raise sqlite3.OperationalError("example commit fault")

        with patch.object(self.state, "_connection", cast(Any, CommitFault())):
            with self.assertRaises(sqlite3.OperationalError):
                self.state.terminate_provider_job_with_notice(
                    self.job.job_id,
                    lease.lease_token,
                    status="failed",
                    error_class="pre_execution",
                    error_code="CodexPreparationError",
                    sender_agent_id="codex",
                    telegram_html="Example preparation failure",
                    preparation_retry=PreparationRetryBinding(self.harness.root, None),
                )
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "executing")
        self.assertIsNone(self.sql("SELECT 1 FROM telegram_outbox LIMIT 1").fetchone())
        self.assertIsNone(
            self.sql("SELECT 1 FROM provider_preexecution_retry_tickets LIMIT 1").fetchone()
        )

    def test_material_membership_refuses_even_after_discard(self) -> None:
        material = self.add_material(self.job.job_id)
        self.preparation_failure(self.job.job_id, 101)
        self.assertNotIn(
            "Reply exactly retry",
            self.state.get_telegram_outbox_for_job(self.job.job_id).telegram_html,
        )
        self.state.mark_incoming_materials_discarded(
            [material], code="example_discarded", detail="Example discarded"
        )
        with self.assertRaisesRegex(PreparationRetryRefused, "materials"):
            self.retry(self.job.job_id, 101)
        self.assertEqual(len(self.state.provider_jobs_for_topic(self.topic.topic_id)), 1)

    def add_material(
        self, job_id: str | None, *, message_id: int = 20, origin: str = "topic"
    ) -> str:
        draft = IncomingMaterialDraft(
            1,
            None,
            "document",
            None,
            None,
            "example.txt",
            None,
            None,
            None,
            None,
            None,
            "unavailable",
            "example_unavailable",
            "Example unavailable",
        )
        with self.state._immediate_transaction():
            self.state._insert_incoming_materials(
                job_id=job_id,
                topic_id=self.topic.topic_id,
                chat_id=CHAT_ID,
                message_id=message_id,
                agent_id="codex",
                session_id=self.session.session_id,
                session_generation=self.session.generation,
                materials=(draft,),
                timestamp=datetime.now(timezone.utc).isoformat(),
                origin=origin,
            )
        return self.sql(
            "SELECT material_id FROM incoming_materials WHERE chat_id=? AND message_id=?",
            (CHAT_ID, message_id),
        ).fetchone()[0]

    def test_pending_forward_is_not_attached_and_following_burst_does_not_extend_retry(
        self,
    ) -> None:
        self.preparation_failure(self.job.job_id, 101)
        pending = self.add_material(None, message_id=25, origin="forward")
        child, _ = self.retry(self.job.job_id, 101)
        self.assertEqual(self.state.incoming_materials_for_job(child.job_id), ())
        self.assertIsNone(child.input_group_key)
        self.assertIsNone(child.next_attempt_at)
        self.assertIsNone(
            self.sql(
                "SELECT job_id FROM incoming_materials WHERE material_id=?", (pending,)
            ).fetchone()[0]
        )
        following, created = self.state.enqueue_or_append_provider_job(
            idempotency_key="example:later",
            chat_id=CHAT_ID,
            message_id=32,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Example separate task",
            appended_user_text="Example separate task",
            quiet_ms=1000,
            max_ms=2000,
        )
        self.assertTrue(created)
        self.assertNotEqual(following.job_id, child.job_id)
        self.assertEqual(self.state.get_provider_job(child.job_id).payload_text, self.payload)

    def test_retry_does_not_bypass_an_earlier_hold(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        earlier, _ = self.state.enqueue_provider_job(
            idempotency_key="example:held",
            chat_id=CHAT_ID,
            message_id=22,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Example held task",
        )
        self.sql(
            "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at) VALUES (?,?,?)",
            (earlier.job_id, self.job.job_id, datetime.now(timezone.utc).isoformat()),
        )
        child, _ = self.retry(self.job.job_id, 101)
        self.assertGreater(child.topic_sequence, earlier.topic_sequence)
        self.assertIsNone(self.state.lease_provider_job("codex", "example-worker"))

    def test_retry_is_not_steered_into_another_active_turn(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        parent, _ = self.state.enqueue_provider_job(
            idempotency_key="example:active",
            chat_id=CHAT_ID,
            message_id=22,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Example active task",
        )
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(parent.job_id, lease.lease_token)
        child, _ = self.retry(self.job.job_id, 101)
        self.assertIsNone(self.state.lease_steer_followup(parent.job_id, "example-steer"))
        self.assertEqual(self.state.get_provider_job(child.job_id).status, "queued")

    def test_plain_retry_and_ordinary_reply_remain_new_text(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        self.assertTrue(self.harness.send("retry", message_id=30))
        update = text_update(31, "run it")
        cast(dict[str, Any], update["message"])["reply_to_message"] = {"message_id": 101}
        self.assertTrue(self.harness.service.handle_update(update))
        self.assertEqual(
            [job.payload_text for job in self.state.provider_jobs_for_topic(self.topic.topic_id)],
            [self.payload, "retry", "run it"],
        )

    def test_transient_cause_requires_exact_preparation_type(self) -> None:
        for cause, eligible in (
            (RpcError("Codex notification buffer exceeded its bound"), True),
            (TimeoutError("Example timeout"), True),
            (RpcRejectedError("Codex notification buffer exceeded its bound"), False),
            (RpcError("Example policy refusal"), False),
            (StateError("Example integrity failure"), False),
        ):
            error = CodexPreparationError(str(cause))
            error.__cause__ = cause
            self.assertEqual(
                preparation_retry_binding(error, root=self.harness.root, model_provider=None)
                is not None,
                eligible,
            )
        unsupported = UnsupportedCodexPermissionProfileError()
        unsupported.__cause__ = TimeoutError()
        self.assertIsNone(
            preparation_retry_binding(unsupported, root=self.harness.root, model_provider=None)
        )
        self.assertIsNone(
            preparation_retry_binding(
                CodexPreparationError("no cause"), root=self.harness.root, model_provider=None
            )
        )

    def test_exact_retry_thread_cannot_be_replaced_by_fallback(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        child, _ = self.retry(self.job.job_id, 101)
        with self.assertRaises(CodexRetryBindingError):
            require_exact_retry_transport(child, True)
        require_exact_retry_transport(child, False)

    def test_covering_stop_cancels_only_without_contradictory_execution_evidence(self) -> None:
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                job = self.job
                if accepted:
                    job, _ = self.state.enqueue_provider_job(
                        idempotency_key="example:contradiction",
                        chat_id=CHAT_ID,
                        message_id=23,
                        topic_id=self.topic.topic_id,
                        agent_id="codex",
                        session_id=self.session.session_id,
                        session_generation=self.session.generation,
                        model=self.session.model,
                        effort=self.session.effort,
                        payload_text="Example contradictory task",
                    )
                lease = self.state.lease_provider_job("codex", "example-worker")
                assert lease is not None and lease.lease_token is not None
                self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
                if accepted:
                    journal = ExecutionJournal(self.state)
                    journal.record_thread(
                        job.job_id, lease.lease_token, "example-accepted-thread", self.harness.root
                    )
                    journal.record_turn(job.job_id, lease.lease_token, "example-accepted-turn")
                self.state.request_emergency_stop(
                    topic_id=self.topic.topic_id,
                    chat_id=CHAT_ID,
                    message_id=40 + int(accepted),
                    target_agent_id="codex",
                )
                result = self.state.terminate_provider_job_with_notice(
                    job.job_id,
                    lease.lease_token,
                    status="failed",
                    error_class="pre_execution",
                    error_code="CodexPreparationError",
                    sender_agent_id="codex",
                    telegram_html="Example failure",
                    preparation_retry=PreparationRetryBinding(self.harness.root, None),
                )
                self.assertEqual(result.status, "indeterminate" if accepted else "cancelled")
                self.assertIsNone(
                    self.sql(
                        "SELECT 1 FROM provider_preexecution_retry_tickets WHERE source_job_id=?",
                        (job.job_id,),
                    ).fetchone()
                )
                if accepted:
                    self.assertIn(
                        "conflicts",
                        self.state.get_telegram_outbox_for_job(job.job_id).telegram_html,
                    )
                    self.assertEqual(
                        self.state.execution_capacity_snapshot(1)["blocked_uncertain_scopes"], 1
                    )

    def test_legacy_failure_without_ticket_is_visible_refusal(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        self.sql("DELETE FROM provider_preexecution_retry_tickets")
        update = text_update(30, "retry")
        cast(dict[str, Any], update["message"])["reply_to_message"] = {"message_id": 101}
        self.assertTrue(self.harness.service.handle_update(update))
        self.assertIn("no verified saved retry binding", self.harness.last_reply)
        self.assertEqual(len(self.state.provider_jobs_for_topic(self.topic.topic_id)), 1)

    def test_local_writer_on_same_root_blocks_retry_without_consuming_it(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        other = self.state.observe_topic(
            project_id="example-project",
            chat_id=CHAT_ID,
            thread_id=THREAD_ID + 1,
            title="Example other",
            execution_root=self.harness.root,
        )
        writer = self.state.activate_agent(
            other.topic_id, "codex", self.session.model, self.session.effort
        )
        self.state.set_writer_mode(writer.session_id, "local")
        with self.assertRaises(StateError):
            self.retry(self.job.job_id, 101)
        self.assertFalse(self.state.message_already_observed(CHAT_ID, 30))
        self.assertEqual(len(self.state.provider_jobs_for_topic(self.topic.topic_id)), 1)

    def test_profile_change_refuses_saved_retry_and_worker_access(self) -> None:
        self.preparation_failure(self.job.job_id, 101)
        with patch.object(self.state, "_codex_permission_profile_context", "example-managed"):
            with self.assertRaises(PreparationRetryRefused):
                self.retry(self.job.job_id, 101)
        child, _ = self.retry(self.job.job_id, 101)
        with self.assertRaises(CodexPreparationError):
            validate_codex_worker_binding(
                self.state,
                child,
                replace(self.harness.service.config, codex_permission_profile="example-managed"),
                self.harness.root,
            )

    def test_source_context_nullable_handoff_and_original_input_membership_are_preserved(
        self,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.sql(
            "INSERT INTO external_turn_excerpts(topic_id,agent_id,user_excerpt,response_excerpt,created_at) "
            "VALUES (?,'example-advisor','Example question','Example answer',?)",
            (self.topic.topic_id, now),
        )
        watermark = cursor.lastrowid
        self.sql(
            "UPDATE provider_jobs SET context_watermark=? WHERE job_id=?",
            (watermark, self.job.job_id),
        )
        self.sql(
            "INSERT INTO provider_job_inputs VALUES (?,?,?,2,'Example original authorization',?)",
            (self.job.job_id, CHAT_ID, 21, now),
        )
        inputs = tuple(
            tuple(row)
            for row in self.sql(
                "SELECT * FROM provider_job_inputs WHERE job_id=?", (self.job.job_id,)
            )
        )
        self.preparation_failure(self.job.job_id, 101)
        child, _ = self.retry(self.job.job_id, 101)
        self.assertEqual((child.context_watermark, child.handoff_id), (watermark, None))
        self.assertEqual(
            tuple(
                tuple(row)
                for row in self.sql(
                    "SELECT * FROM provider_job_inputs WHERE job_id=?", (self.job.job_id,)
                )
            ),
            inputs,
        )
        self.assertEqual(
            self.sql(
                "SELECT COUNT(*) FROM provider_job_inputs WHERE job_id=?", (child.job_id,)
            ).fetchone()[0],
            1,
        )

    def test_external_worker_two_preparation_failures_then_success_preserves_task(self) -> None:
        class Client(worker_fixtures.WorkerClient):
            attempts = 0
            prompts: list[str] = []

            def start_thread(self, **kwargs):
                self.attempts += 1
                if self.attempts <= 2:
                    raise RpcError("Codex notification buffer exceeded its bound")
                return super().start_thread(**kwargs)

            def start_turn(self, **kwargs):
                self.prompts.append(kwargs["text"])
                return super().start_turn(**kwargs)

            def respond_permission(self, *_args, **_kwargs):
                raise AssertionError("task authorization cannot grant a native permission")

        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        client = Client()
        worker = fixture.worker(client)
        self.addCleanup(worker.close)
        source = fixture.enqueue(payload=self.payload)
        original = worker.state.get_provider_job(source)
        for attempt in range(2):
            self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(source).status, "failed")
            self.assertEqual(client.turns, 0)
            delivery = worker.state.lease_telegram_outbox("codex", "example-sender")
            assert delivery is not None and delivery.lease_token is not None
            notice_id = 101 + attempt
            worker.state.mark_telegram_outbox_delivered(
                delivery.outbox_id,
                delivery.lease_token,
                telegram_message_id=notice_id,
            )
            child, created = PreexecutionRetryState(worker.state).retry_from_notice(
                source_job_id=source,
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                notice_message_id=notice_id,
                reply_message_id=30 + attempt,
                canonical_root=fixture.registry.require_project("example-project").root,
                model_provider=None,
            )
            self.assertTrue(created)
            self.assertEqual(child.payload_text, self.payload)
            source = child.job_id
        self.assertTrue(worker.run_cycle())
        self.assertEqual(client.turns, 1)
        self.assertEqual(len(client.prompts), 1)
        self.assertIn(self.payload, client.prompts[0])
        self.assertEqual(worker.state.get_provider_job(source).status, "result_ready")
        self.assertEqual(worker.state.get_provider_job(original.job_id).payload_text, self.payload)

    def test_fallback_bridge_after_binding_fault_cannot_retry_short_authorization(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        source = fixture.enqueue(payload="Run it", provider_session_id="example-original-thread")
        client = worker_fixtures.WorkerClient()
        supervisor = worker_fixtures.WorkerSupervisor(client)
        supervisor.transport_mode = "stdio-fallback"
        worker = worker_fixtures.CodexQueueWorker(
            fixture.config, registry=fixture.registry, supervisor=cast(Any, supervisor)
        )
        self.addCleanup(worker.close)
        state = worker.state
        original = state.get_provider_job(source)
        state.record_visible_turn(
            original.topic_id,
            agent_id="codex",
            provider="codex",
            model=original.model,
            user_excerpt="Approved: run all six fictional scenarios",
            response_excerpt=self.payload,
            provider_session_id="example-original-thread",
        )
        record_thread = ExecutionJournal.record_thread

        def bind_then_fail(journal, *args, **kwargs):
            record_thread(journal, *args, **kwargs)
            raise RpcError("Codex notification buffer exceeded its bound")

        # Fault injection explores this boundary; it does not establish a live trigger.
        with (
            patch.object(ExecutionJournal, "record_thread", bind_then_fail),
            patch(
                "hermes_codex_router.external_worker.codex_turn_text", wraps=codex_turn_text
            ) as prepared_text,
        ):
            self.assertTrue(worker.run_cycle())
        self.assertIn(self.payload, prepared_text.call_args.kwargs["fallback_visible_context"])
        failed = state.get_provider_job(source)
        self.assertEqual(failed.error_code, "CodexPreparationError")
        self.assertEqual(failed.payload_text, "Run it")
        self.assertEqual(failed.provider_session_id, "example-original-thread")
        self.assertEqual(state.get_session(original.session_id).provider_session_id, "thread-1")
        delivery = state.lease_telegram_outbox("codex", "example-sender")
        assert delivery is not None and delivery.lease_token is not None
        self.assertIn("no saved context snapshot", delivery.telegram_html)
        self.assertNotIn("Reply exactly retry", delivery.telegram_html)
        state.mark_telegram_outbox_delivered(
            delivery.outbox_id, delivery.lease_token, telegram_message_id=101
        )
        state.record_visible_turn(
            original.topic_id,
            agent_id="codex",
            provider="codex",
            model=original.model,
            user_excerpt="Later unrelated question",
            response_excerpt="Later unrelated answer",
            provider_session_id="thread-1",
        )
        supervisor.transport_mode = "socket"
        with self.assertRaisesRegex(PreparationRetryRefused, "no verified saved retry binding"):
            PreexecutionRetryState(state).retry_from_notice(
                source_job_id=source,
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                notice_message_id=101,
                reply_message_id=30,
                canonical_root=fixture.registry.require_project("example-project").root,
                model_provider=None,
            )
        self.assertEqual(client.turns, 0)
        self.assertEqual(len(state.provider_jobs_for_topic(original.topic_id)), 1)
