"""Immutable ingress follows admission, never labels, repeats or historical targets."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict, cast
from unittest.mock import patch

from hermes_codex_router.codex_retry_policy import PreparationRetryBinding
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import StateError
from hermes_codex_router.state_provider_jobs import ProviderJobRecord
from hermes_codex_router.telegram import parse_topic_message
from tests.delivery_fixture import complete_final_delivery
from tests.hub_service_harness import CHAT_ID, CODEX, OWNER_ID, HubHarness, text_update


class AdmissionOptions(TypedDict):
    idempotency_key: str
    chat_id: int
    message_id: int
    topic_id: int
    agent_id: str
    session_id: str
    session_generation: int
    model: str
    effort: str
    payload_text: str
    telegram_ingress_identity: str | None


def row_values(row: sqlite3.Row | None) -> dict[str, Any]:
    assert row is not None
    return {key: row[key] for key in row.keys()}


class TelegramTurnProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.harness = HubHarness(Path(temporary.name))
        self.addCleanup(self.harness.close)
        self.state = self.harness.service.state
        self.session = self.harness.activate(CODEX, provider_session_id=None)
        self.topic = self.harness.topic()
        self.next_message = 20

    def enqueue(
        self,
        ingress: str | None = None,
        *,
        message: int | None = None,
        batch: bool = False,
        available_at: datetime | None = None,
    ) -> tuple[ProviderJobRecord, bool]:
        if message is None:
            message = self.next_message
            self.next_message += 1
        options = AdmissionOptions(
            idempotency_key=f"example-input:{message}",
            chat_id=CHAT_ID,
            message_id=message,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text=f"Example task {message}",
            telegram_ingress_identity=ingress,
        )
        if batch:
            return self.state.enqueue_or_append_provider_job(
                **options,
                appended_user_text=f"Example follow-up {message}",
                quiet_ms=10000,
                max_ms=20000,
            )
        return self.state.enqueue_provider_job(**options, available_at=available_at)

    def test_explicit_ingress_is_distinct_from_provider_and_observer(self):
        known, _ = self.enqueue("hub")
        unknown, _ = self.enqueue()
        self.assertEqual(self.state.telegram_turn_provenance.identity(known.job_id), "hub")
        self.assertIsNone(self.state.telegram_turn_provenance.identity(unknown.job_id))
        observer = self.state._connection.execute(
            "SELECT observer_agent_id FROM observed_messages WHERE chat_id=? AND message_id=?",
            (CHAT_ID, unknown.message_id),
        ).fetchone()[0]
        self.assertEqual(observer, "hub")
        self.assertIsNone(self.state.telegram_turn_provenance.target(known.job_id))

    def test_duplicate_admission_cannot_enrich_unknown_or_retarget_known(self):
        unknown, _ = self.enqueue(message=20)
        again, created = self.enqueue("hub", message=20)
        self.assertFalse(created)
        self.assertEqual(again.job_id, unknown.job_id)
        self.assertIsNone(self.state.telegram_turn_provenance.identity(unknown.job_id))
        known, _ = self.enqueue("hub", message=21)
        again, created = self.enqueue("codex", message=21)
        self.assertFalse(created)
        self.assertEqual(again.job_id, known.job_id)
        self.assertEqual(self.state.telegram_turn_provenance.identity(known.job_id), "hub")

    def test_batch_matrix_preserves_fifo_and_unknown_legacy_behavior(self):
        for old_ingress in (None, "hub", "codex"):
            for incoming in (None, "hub", "codex"):
                with self.subTest(old=old_ingress, incoming=incoming):
                    old, _ = self.enqueue(
                        old_ingress, available_at=datetime.now(timezone.utc) + timedelta(seconds=10)
                    )
                    new, created = self.enqueue(incoming, batch=True)
                    self.assertTrue(created)
                    self.assertEqual(new.job_id == old.job_id, old_ingress == incoming)
                    self.assertEqual(
                        self.state.telegram_turn_provenance.identity(old.job_id), old_ingress
                    )
                    self.assertEqual(
                        self.state.telegram_turn_provenance.identity(new.job_id), incoming
                    )

                    repeated, created = self.enqueue(
                        "codex", message=self.next_message - 1, batch=True
                    )
                    self.assertFalse(created)
                    self.assertEqual(repeated.job_id, new.job_id)
                    self.assertEqual(
                        self.state.telegram_turn_provenance.identity(new.job_id), incoming
                    )

    def test_steering_ingress_matrix_and_direct_start_recheck(self):
        for old_ingress in (None, "hub", "codex"):
            for incoming in (None, "hub", "codex"):
                with self.subTest(old=old_ingress, incoming=incoming):
                    fixture = TelegramTurnProvenanceTests()
                    fixture.setUp()
                    self.addCleanup(fixture.doCleanups)
                    parent, _ = fixture.enqueue(old_ingress)
                    fixture.accept(parent)
                    child, _ = fixture.enqueue(incoming)
                    state = fixture.state
                    lease = state.lease_steer_followup(parent.job_id, "example-worker")
                    self.assertEqual(lease is not None, old_ingress == incoming)
                    if lease is None:
                        # Bypass the selector to verify the transaction immediately before RPC.
                        with state._immediate_transaction():
                            state._connection.execute(
                                """UPDATE provider_jobs SET status='leased',lease_owner='example-worker',
                                   lease_token='example-bypass',lease_expires_at=? WHERE job_id=?""",
                                (
                                    (
                                        datetime.now(timezone.utc) + timedelta(seconds=90)
                                    ).isoformat(),
                                    child.job_id,
                                ),
                            )
                        token = "example-bypass"
                    else:
                        assert lease.lease_token is not None
                        token = lease.lease_token
                    started = state.start_steer_followup(
                        child.job_id, token, parent_job_id=parent.job_id
                    )
                    self.assertEqual(
                        started.status, "executing" if old_ingress == incoming else "queued"
                    )
                    self.assertEqual(started.attempt_count, int(old_ingress == incoming))
                    if old_ingress != incoming:
                        self.assertIsNone(started.provider_started_at)
                    self.assertEqual(
                        state.telegram_turn_provenance.identity(parent.job_id), old_ingress
                    )
                    self.assertEqual(
                        state.telegram_turn_provenance.identity(child.job_id), incoming
                    )

    def test_stop_cancellation_precedes_steer_ingress_mismatch(self):
        parent, _ = self.enqueue("hub")
        self.accept(parent)
        child, _ = self.enqueue("codex")
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """UPDATE provider_jobs SET status='leased',lease_owner='example-worker',
                   lease_token='example-bypass',lease_expires_at=? WHERE job_id=?""",
                ((datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat(), child.job_id),
            )
        self.state.request_emergency_stop(
            topic_id=self.topic.topic_id, chat_id=CHAT_ID, message_id=99, target_agent_id="codex"
        )
        self.assertIsNone(self.state.lease_steer_followup(parent.job_id, "example-worker"))
        started = self.state.start_steer_followup(
            child.job_id, "example-bypass", parent_job_id=parent.job_id
        )
        self.assertEqual((started.status, started.attempt_count), ("cancelled", 0))
        self.assertIsNone(started.provider_started_at)

    def test_service_uses_only_trusted_explicit_group_ingress(self):
        service = self.harness.service
        message = parse_topic_message(text_update(20, "Example task"))
        assert message is not None
        for identity in (None, "hub", "codex", "claude"):
            for dm in (False, True):
                for owns_group in (False, True):
                    with (
                        self.subTest(identity=identity, dm=dm, owner=owns_group),
                        patch.object(service, "ingress_identity", identity, create=True),
                    ):
                        service.direct_messages_only = dm
                        service._publishes_controller_health = owns_group
                        self.assertEqual(
                            service._telegram_group_ingress(message),
                            identity
                            if owns_group and not dm and identity in {"hub", "codex"}
                            else None,
                        )

    def test_actual_controller_admission_keeps_ingress_separate_from_codex_provider(self):
        service = self.harness.service
        service.ingress_identity = "hub"
        service._publishes_controller_health = True
        self.assertTrue(service.handle_update(text_update(20, "Example task")))
        job = self.state.provider_jobs_for_topic(self.topic.topic_id)[0]
        self.assertEqual(job.agent_id, "codex")
        self.assertEqual(self.state.telegram_turn_provenance.identity(job.job_id), "hub")
        service.ingress_identity = "codex"
        self.assertFalse(service.handle_update(text_update(20, "Example task")))
        self.assertEqual(self.state.telegram_turn_provenance.identity(job.job_id), "hub")
        with patch.object(service, "ingress_identity", None):
            self.assertTrue(service.handle_update(text_update(21, "Example next task")))
        jobs = self.state.provider_jobs_for_topic(self.topic.topic_id)
        self.assertEqual(len(jobs), 2)
        self.assertIsNone(self.state.telegram_turn_provenance.identity(jobs[1].job_id))
        self.assertEqual(self.harness.client.turns, 0)

    def test_missing_group_ownership_cannot_establish_ingress(self):
        service = self.harness.service
        service.ingress_identity = "hub"
        service.__dict__.pop("_publishes_controller_health", None)
        message = parse_topic_message(text_update(20, "Example task"))
        assert message is not None
        self.assertIsNone(service._telegram_group_ingress(message))

    def test_private_unmatched_retry_reply_remains_ordinary_input(self):
        self.harness.with_config(direct_message_project_id="example-project")
        service = self.harness.service
        service.ingress_identity = "codex"
        service._publishes_controller_health = True
        service.direct_messages_only = False
        update = text_update(20, "retry")
        message = cast(dict[str, Any], update["message"])
        message["chat"] = {"id": OWNER_ID, "type": "private"}
        message.pop("is_topic_message")
        message.pop("message_thread_id")
        message["reply_to_message"] = {
            "message_id": 101,
            "from": {"is_bot": True, "username": "example_antigravity_bot"},
        }
        self.assertTrue(service.handle_update(update))
        topic = self.state.find_topic(OWNER_ID, 1)
        assert topic is not None
        jobs = self.state.provider_jobs_for_topic(topic.topic_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].payload_text, jobs[0].agent_id), ("retry", "codex"))
        self.assertIsNone(self.state.telegram_turn_provenance.identity(jobs[0].job_id))
        self.assertEqual(self.harness.client.turns, 0)

    def test_mixed_codex_controller_private_admission_and_retry_have_no_group_provenance(self):
        self.harness.with_config(direct_message_project_id="example-project")
        service = self.harness.service
        service.ingress_identity = "codex"
        service._publishes_controller_health = True
        service.direct_messages_only = False

        def private_update(message_id, text, *, reply=None):
            update = text_update(message_id, text)
            message = cast(dict[str, Any], update["message"])
            message["chat"] = {"id": OWNER_ID, "type": "private"}
            message.pop("is_topic_message")
            message.pop("message_thread_id")
            if reply is not None:
                message["reply_to_message"] = {"message_id": reply}
            return update

        self.assertTrue(service.handle_update(private_update(20, "Example approved task")))
        topic = self.state.find_topic(OWNER_ID, 1)
        assert topic is not None
        source = self.state.provider_jobs_for_topic(topic.topic_id)[0]
        self.assertIsNone(self.state.telegram_turn_provenance.identity(source.job_id))
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(source.job_id, lease.lease_token)
        self.state.terminate_provider_job_with_notice(
            source.job_id,
            lease.lease_token,
            status="failed",
            error_class="pre_execution",
            error_code="CodexPreparationError",
            sender_agent_id="codex",
            telegram_html="Example preparation failure",
            provider_runtime="codex",
            preparation_retry=PreparationRetryBinding(self.harness.root, None),
        )
        delivery = self.state.lease_telegram_outbox("codex", "example-sender")
        assert delivery is not None and delivery.lease_token is not None
        complete_final_delivery(
            self.state, delivery.outbox_id, delivery.lease_token, telegram_message_id=101
        )
        self.assertTrue(service.handle_update(private_update(30, "retry", reply=101)))
        jobs = self.state.provider_jobs_for_topic(topic.topic_id)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[1].payload_text, "Example approved task")
        self.assertIsNone(self.state.telegram_turn_provenance.identity(jobs[1].job_id))
        self.assertTrue(service.handle_update(private_update(31, "retry", reply=101)))
        repeated = self.state.provider_jobs_for_topic(topic.topic_id)
        self.assertEqual(len(repeated), 2)
        self.assertEqual(repeated[1].job_id, jobs[1].job_id)
        self.assertEqual(repeated[1].payload_text, source.payload_text)
        self.assertIsNone(self.state.telegram_turn_provenance.identity(repeated[1].job_id))
        self.assertEqual(self.harness.client.turns, 0)

    def test_provenance_writes_cannot_commit_outside_the_owning_transaction(self):
        job, _ = self.enqueue()
        self.assertFalse(self.state._connection.in_transaction)
        with self.assertRaises(StateError):
            self.state.telegram_turn_provenance.record_new_job_in_transaction(job.job_id, "hub")
        with self.assertRaises(StateError):
            self.state.telegram_turn_provenance.record_fresh_target_in_transaction(job.job_id)
        self.assertIsNone(self.state.telegram_turn_provenance.identity(job.job_id))
        self.assertIsNone(self.state.telegram_turn_provenance.target(job.job_id))

    def test_refused_then_repeated_acceptance_never_upgrades_telegram_target(self):
        job, _ = self.enqueue("hub")
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(self.state)
        journal.record_thread(job.job_id, lease.lease_token, "example-thread", self.harness.root)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (job.session_id,),
            )
        with self.assertRaises(StateError):
            journal.record_turn(job.job_id, lease.lease_token, "example-turn")
        checkpoint = journal.read(job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["provider_turn_id"], "example-turn")
        self.assertIsNone(self.state.codex_controls.read(job.job_id))
        self.assertIsNone(self.state.telegram_turn_provenance.target(job.job_id))
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='telegram' WHERE session_id=?",
                (job.session_id,),
            )
        journal.record_turn(job.job_id, lease.lease_token, "example-turn")
        self.assertIsNone(self.state.telegram_turn_provenance.target(job.job_id))

    def accept(self, job):
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.assertEqual(lease.job_id, job.job_id)
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(self.state)
        journal.record_thread(job.job_id, lease.lease_token, "example-thread", self.harness.root)
        journal.record_turn(job.job_id, lease.lease_token, "example-turn")
        return journal, lease.lease_token

    def test_fresh_coherent_acceptance_freezes_target_but_grants_no_interrupt(self):
        job, _ = self.enqueue("hub")
        journal, token = self.accept(job)
        target = self.state.telegram_turn_provenance.target(job.job_id)
        assert target is not None
        self.assertEqual(target["ingress_identity"], "hub")
        control = self.state.codex_controls.read(job.job_id)
        assert control is not None
        self.assertIsNone(control["send_started_at"])
        journal.record_turn(job.job_id, token, "example-turn")
        self.assertEqual(
            row_values(self.state.telegram_turn_provenance.target(job.job_id)), dict(target)
        )

    def test_unknown_ingress_keeps_stage2_acceptance_without_telegram_target(self):
        job, _ = self.enqueue()
        self.accept(job)
        self.assertIsNotNone(self.state.codex_controls.read(job.job_id))
        self.assertIsNone(self.state.telegram_turn_provenance.target(job.job_id))

    def test_sidecar_storage_fault_rolls_back_the_entire_admission(self):
        self.state._connection.execute(
            """CREATE TEMP TRIGGER example_refuse_ingress BEFORE INSERT ON provider_job_telegram_ingress
               BEGIN SELECT RAISE(ABORT, 'example ingress fault'); END"""
        )
        self.state._connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.enqueue("hub", message=50)
        self.assertIsNone(
            self.state._connection.execute(
                "SELECT 1 FROM provider_jobs WHERE message_id=50"
            ).fetchone()
        )
        self.assertFalse(self.state.message_already_observed(CHAT_ID, 50))

    def test_target_storage_fault_rolls_back_control_and_native_checkpoint(self):
        job, _ = self.enqueue("hub")
        self.state._connection.execute(
            """CREATE TEMP TRIGGER example_refuse_target BEFORE INSERT ON codex_telegram_precaution_targets
               BEGIN SELECT RAISE(ABORT, 'example target fault'); END"""
        )
        self.state._connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.accept(job)
        checkpoint = ExecutionJournal(self.state).read(job.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertIsNone(self.state.codex_controls.read(job.job_id))
        self.assertIsNone(self.state.telegram_turn_provenance.target(job.job_id))

    def test_sidecars_and_bound_job_identity_cannot_be_replaced_or_mutated(self):
        job, _ = self.enqueue("hub")
        self.accept(job)
        for table in ("provider_job_telegram_ingress", "codex_telegram_precaution_targets"):
            for query in (
                f"UPDATE {table} SET ingress_identity='codex' WHERE job_id=?",
                f"DELETE FROM {table} WHERE job_id=?",
                f"INSERT OR REPLACE INTO {table} VALUES (?,'codex')",
            ):
                with self.subTest(query=query):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.state._connection.execute(query, (job.job_id,))
                    self.state._connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.state._connection.execute(
                "UPDATE provider_jobs SET message_id=999 WHERE job_id=?", (job.job_id,)
            )
        self.state._connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.state._connection.execute(
                "INSERT OR REPLACE INTO provider_jobs SELECT * FROM provider_jobs WHERE job_id=?",
                (job.job_id,),
            )
        self.state._connection.rollback()
        self.assertEqual(self.state.telegram_turn_provenance.identity(job.job_id), "hub")

    def test_historical_input_closure_cannot_be_reopened_for_enrichment(self):
        job, _ = self.enqueue()
        with self.assertRaises(StateError):
            with self.state._immediate_transaction():
                self.state.telegram_turn_provenance.record_new_job_in_transaction(job.job_id, "hub")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state._connection.execute(
                "DELETE FROM provider_job_inputs WHERE job_id=? AND part_index=1", (job.job_id,)
            )
        self.state._connection.rollback()
        self.assertIsNone(self.state.telegram_turn_provenance.identity(job.job_id))

    def test_update_replace_cannot_remove_another_jobs_first_input(self):
        victim, _ = self.enqueue()
        donor, _ = self.enqueue()
        db = self.state._connection
        db.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        db.execute("PRAGMA recursive_triggers=OFF")
        db.execute(
            """INSERT INTO provider_job_inputs
               SELECT job_id,chat_id,999,2,input_text,received_at
               FROM provider_job_inputs WHERE job_id=?""",
            (donor.job_id,),
        )
        db.commit()
        before = [
            tuple(row)
            for row in db.execute("SELECT * FROM provider_job_inputs ORDER BY message_id")
        ]
        for sql, values in (
            (
                "UPDATE OR REPLACE provider_job_inputs SET chat_id=?,message_id=? WHERE message_id=999",
                (CHAT_ID, victim.message_id),
            ),
            (
                "UPDATE OR REPLACE provider_job_inputs SET job_id=?,part_index=1 WHERE message_id=999",
                (victim.job_id,),
            ),
            (
                "UPDATE OR REPLACE provider_job_inputs SET rowid=? WHERE message_id=999",
                (
                    db.execute(
                        "SELECT rowid FROM provider_job_inputs WHERE job_id=?", (victim.job_id,)
                    ).fetchone()[0],
                ),
            ),
            (
                "INSERT OR REPLACE INTO provider_job_inputs (rowid,job_id,chat_id,message_id,part_index,input_text,received_at) SELECT ?,job_id,chat_id,message_id,part_index,input_text,received_at FROM provider_job_inputs WHERE message_id=999",
                (
                    db.execute(
                        "SELECT rowid FROM provider_job_inputs WHERE job_id=?", (victim.job_id,)
                    ).fetchone()[0],
                ),
            ),
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql, values)
                db.rollback()
                self.assertEqual(
                    [
                        tuple(row)
                        for row in db.execute(
                            "SELECT * FROM provider_job_inputs ORDER BY message_id"
                        )
                    ],
                    before,
                )
                with self.assertRaises(StateError):
                    with self.state._immediate_transaction():
                        self.state.telegram_turn_provenance.record_new_job_in_transaction(
                            victim.job_id, "hub"
                        )

    def test_update_replace_cannot_take_over_a_bound_job_identity(self):
        victim, _ = self.enqueue("hub")
        db = self.state._connection
        db.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        db.execute("PRAGMA recursive_triggers=OFF")
        source = dict(
            db.execute("SELECT * FROM provider_jobs WHERE job_id=?", (victim.job_id,)).fetchone()
        )
        donor = source | dict(
            job_id="example-provisional",
            idempotency_key="example-provisional",
            message_id=999,
            topic_sequence=999,
            payload_text="Example provisional input",
        )
        columns = ",".join(donor)
        placeholders = ",".join("?" for _ in donor)
        db.execute(
            f"INSERT INTO provider_jobs ({columns}) VALUES ({placeholders})", tuple(donor.values())
        )
        db.commit()
        for assignment, values in (
            ("job_id=?", (victim.job_id,)),
            ("idempotency_key=?", (victim.idempotency_key,)),
            ("chat_id=?,message_id=?", (victim.chat_id, victim.message_id)),
            ("topic_id=?,topic_sequence=?", (victim.topic_id, victim.topic_sequence)),
            (
                "rowid=?",
                (
                    db.execute(
                        "SELECT rowid FROM provider_jobs WHERE job_id=?", (victim.job_id,)
                    ).fetchone()[0],
                ),
            ),
        ):
            with self.subTest(assignment=assignment):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        f"UPDATE OR REPLACE provider_jobs SET {assignment} WHERE job_id='example-provisional'",
                        values,
                    )
                db.rollback()
                self.assertEqual(
                    dict(
                        db.execute(
                            "SELECT * FROM provider_jobs WHERE job_id=?", (victim.job_id,)
                        ).fetchone()
                    ),
                    source,
                )
                self.assertEqual(self.state.telegram_turn_provenance.identity(victim.job_id), "hub")

    def test_insert_replace_cannot_retarget_controls_or_reset_send_fence(self):
        for ingress in (None, "hub"):
            with self.subTest(ingress=ingress):
                # Independent fixture keeps FIFO and immutable native identities distinct.
                fixture = TelegramTurnProvenanceTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                job, _ = fixture.enqueue(ingress)
                fixture.accept(job)
                db = fixture.state._connection
                db.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                db.execute("PRAGMA recursive_triggers=OFF")
                db.execute(
                    """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
                       interrupt_source='protective',interrupt_outcome='unknown' WHERE job_id=?""",
                    (datetime.now(timezone.utc).isoformat(), "a" * 64, job.job_id),
                )
                db.commit()
                control = row_values(fixture.state.codex_controls.read(job.job_id))
                for changed in (
                    control | dict(provider_turn_id="example-substitute-turn"),
                    control
                    | dict(
                        send_started_at=None,
                        send_owner_token_hash=None,
                        interrupt_source=None,
                        interrupt_outcome=None,
                    ),
                ):
                    columns = ",".join(changed)
                    placeholders = ",".join("?" for _ in changed)
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(
                            f"INSERT OR REPLACE INTO codex_turn_controls ({columns}) VALUES ({placeholders})",
                            tuple(changed.values()),
                        )
                    db.rollback()
                    self.assertEqual(
                        row_values(fixture.state.codex_controls.read(job.job_id)), control
                    )
                    self.assertEqual(
                        fixture.state.telegram_turn_provenance.identity(job.job_id), ingress
                    )

    def test_explicit_rowid_and_native_collision_preserve_sidecars_and_control(self):
        victim, _ = self.enqueue("hub")
        self.accept(victim)
        db = self.state._connection
        db.execute("PRAGMA recursive_triggers=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

        def insert(table, values, *, replace=False):
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            verb = "INSERT OR REPLACE" if replace else "INSERT"
            db.execute(
                f"{verb} INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
            )

        donor_id = "example-provisional"
        donor = dict(
            db.execute("SELECT * FROM provider_jobs WHERE job_id=?", (victim.job_id,)).fetchone()
        )
        donor.update(
            job_id=donor_id,
            idempotency_key=donor_id,
            message_id=999,
            topic_sequence=999,
            status="queued",
            attempt_count=0,
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            provider_started_at=None,
        )
        insert("provider_jobs", donor)
        db.commit()
        rowid = db.execute(
            "SELECT rowid FROM provider_job_telegram_ingress WHERE job_id=?", (victim.job_id,)
        ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "new admission"):
            insert(
                "provider_job_telegram_ingress",
                dict(rowid=rowid, job_id=donor_id, ingress_identity="codex"),
                replace=True,
            )
        db.rollback()
        with self.state._immediate_transaction():
            self.state.telegram_turn_provenance.record_new_job_in_transaction(donor_id, "codex")

        saved_checkpoint = ExecutionJournal(self.state).read(victim.job_id)
        assert saved_checkpoint is not None
        checkpoint = dict(saved_checkpoint)
        checkpoint.update(job_id=donor_id, provider_turn_id=None)
        insert("provider_execution_checkpoints", checkpoint)
        db.commit()
        db.execute(
            """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
               interrupt_source='protective',interrupt_outcome='unknown' WHERE job_id=?""",
            (datetime.now(timezone.utc).isoformat(), "a" * 64, victim.job_id),
        )
        db.commit()
        original = row_values(self.state.codex_controls.read(victim.job_id))
        control_rowid = db.execute(
            "SELECT rowid FROM codex_turn_controls WHERE job_id=?", (victim.job_id,)
        ).fetchone()[0]
        for replacement in (
            original | dict(job_id=donor_id),
            original
            | dict(rowid=control_rowid, job_id=donor_id, provider_turn_id="example-donor-turn"),
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Retained control target"):
                insert("codex_turn_controls", replacement, replace=True)
            db.rollback()
            self.assertEqual(row_values(self.state.codex_controls.read(victim.job_id)), original)

        insert(
            "codex_turn_controls",
            original | dict(job_id=donor_id, provider_turn_id="example-donor-turn"),
        )
        db.commit()
        for assignment, value in (
            ("rowid", control_rowid),
            ("job_id", victim.job_id),
            ("provider_turn_id", original["provider_turn_id"]),
        ):
            with self.subTest(assignment=assignment):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        f"UPDATE OR REPLACE codex_turn_controls SET {assignment}=? WHERE job_id=?",
                        (value, donor_id),
                    )
                db.rollback()
                self.assertEqual(
                    row_values(self.state.codex_controls.read(victim.job_id)), original
                )
        target_rowid = db.execute(
            "SELECT rowid FROM codex_telegram_precaution_targets WHERE job_id=?", (victim.job_id,)
        ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fresh coherent acceptance"):
            insert(
                "codex_telegram_precaution_targets",
                dict(rowid=target_rowid, job_id=donor_id, ingress_identity="codex"),
                replace=True,
            )
        db.rollback()
        self.assertEqual(self.state.telegram_turn_provenance.identity(victim.job_id), "hub")
        target = self.state.telegram_turn_provenance.target(victim.job_id)
        assert target is not None
        self.assertEqual(target["ingress_identity"], "hub")


if __name__ == "__main__":
    unittest.main()
