"""Human outcome decisions remain bound to exact saved results, never execution authority."""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.assessment_inputs import OutcomeAssessmentInput
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.progress_delivery import ProgressDeliveryQueue
from hermes_codex_router.state import StateError
from tests import test_outcome_journal as fixtures
from tests.delivery_fixture import complete_final_delivery


class OutcomeAssessmentFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OutcomeJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.job, self.result = self.fixture.complete()

    def request(
        self,
        number: int = 50,
        *,
        reply: int | None = 101,
        text: str = "/assess accepted Проверено API и Файлы",
    ) -> OutcomeAssessmentInput:
        return OutcomeAssessmentInput(
            owner_user_id=42,
            chat_id=self.fixture.topic.chat_id,
            thread_id=self.fixture.topic.thread_id,
            message_id=number,
            reply_message_id=reply,
            text=text,
        )

    def assess(self, request: OutcomeAssessmentInput):
        return self.state.record_outcome_assessment(
            request, project_id="example-project", owner_user_ids=(42,)
        )

    def deliver(self, *, multipart: bool = False) -> None:
        outbox = self.state.get_telegram_outbox_for_job(self.job.job_id)
        if multipart:
            with self.state._connection:
                self.state._connection.execute(
                    "INSERT INTO telegram_outbox_parts(outbox_id,part_index,telegram_html) VALUES (?,2,'Example second part')",
                    (outbox.outbox_id,),
                )
        for receipt in (101, 102) if multipart else (101,):
            leased = self.state.lease_telegram_outbox("codex", "example-sender")
            assert leased is not None and leased.lease_token is not None
            complete_final_delivery(
                self.state, leased.outbox_id, leased.lease_token, telegram_message_id=receipt
            )


class OutcomeAssessmentStateTests(OutcomeAssessmentFixture):
    def test_multiline_tab_and_unicode_reason_preserves_valid_text(self) -> None:
        self.deliver()
        request = self.request(text="/assess accepted Проверено\nAPI\tи файлы 🎯")
        disposition, created = self.assess(request)
        self.assertTrue(created)
        self.assertEqual(disposition.disposition, "applied")
        self.assertEqual(disposition.reason, "Проверено\nAPI\tи файлы 🎯")

    def test_delivered_progress_and_control_are_not_saved_final_targets(self) -> None:
        self.deliver()
        job = self.fixture.enqueue(2)
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(self.state, progress_enabled=True)
        journal.record_thread(
            job.job_id, lease.lease_token, "example-thread", Path(self.fixture.temp.name)
        )
        journal.record_turn(job.job_id, lease.lease_token, "example-turn")
        journal.record_item(job.job_id, lease.lease_token, "example-item", "Working", "commentary")
        progress = ProgressDeliveryQueue(self.state)
        sent = progress.lease("codex", "example-sender")
        assert sent is not None and sent.lease_token is not None
        self.state.delivery.begin_progress_send(sent.progress_id, sent.lease_token)
        progress.mark_delivered(sent.progress_id, sent.lease_token, telegram_message_id=201)
        now = datetime.now(timezone.utc)
        with self.state._immediate_transaction():
            control, _ = self.state.task_notices.prepare_notice_in_transaction(
                event_key="example-control",
                kind="example",
                job_id=job.job_id,
                chat_id=self.fixture.topic.chat_id,
                thread_id=self.fixture.topic.thread_id,
                telegram_html="Example control",
                now=now,
            )
        notice = self.state.task_notices.lease_notice("example-sender", now=now)
        assert notice is not None and notice.lease_token is not None
        self.assertEqual(notice.notice_id, control.notice_id)
        self.state.task_notices.begin_send(notice.notice_id, notice.lease_token, now=now)
        self.state.task_notices.complete_send(
            notice.notice_id, notice.lease_token, telegram_message_id=202, now=now
        )
        before = self.state.get_provider_job(job.job_id)
        for number, reply in ((50, 201), (51, 202)):
            with self.subTest(reply=reply):
                disposition, _ = self.assess(self.request(number, reply=reply))
                self.assertEqual(disposition.refusal_code, "assessment_result_unavailable")
        self.assertEqual(self.state.get_provider_job(job.job_id), before)
        self.assertEqual(self.fixture.read(job.job_id)["acceptance"]["decision"], "unknown")

    def test_delivered_resultless_failure_notice_cannot_be_assessed(self) -> None:
        self.deliver()
        job = self.fixture.enqueue(2)
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.terminate_provider_job_with_notice(
            job.job_id,
            lease.lease_token,
            expected_status="leased",
            status="failed",
            error_class="pre_execution",
            error_code="example-preparation",
            error_detail="Example failure",
            sender_agent_id="codex",
            telegram_html="Example failure notice",
        )
        outbox = self.state.lease_telegram_outbox("codex", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        complete_final_delivery(
            self.state, outbox.outbox_id, outbox.lease_token, telegram_message_id=201
        )
        before = self.state.get_provider_job(job.job_id)
        disposition, _ = self.assess(self.request(reply=201))
        self.assertEqual(disposition.refusal_code, "assessment_result_unavailable")
        outcome = self.fixture.read(job.job_id)
        self.assertIsNone(outcome["result"])
        self.assertTrue(outcome["notice_delivery"]["receipt_provenance_complete"])
        self.assertEqual(outcome["acceptance"]["decision"], "unknown")
        self.assertEqual(self.state.get_provider_job(job.job_id), before)

    def test_ambiguous_saved_final_receipt_cannot_select_either_result(self) -> None:
        self.deliver()
        job = self.fixture.enqueue(2)
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        self.state.commit_provider_result(
            job.job_id,
            lease.lease_token,
            visible_response="Example second final",
            sender_agent_id="codex",
            telegram_html="Example second final",
        )
        outbox = self.state.lease_telegram_outbox("codex", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        # Deliberately corrupted transport evidence: two finals share a receipt.
        complete_final_delivery(
            self.state, outbox.outbox_id, outbox.lease_token, telegram_message_id=101
        )
        disposition, _ = self.assess(self.request())
        self.assertEqual(disposition.refusal_code, "assessment_result_unavailable")
        self.assertIsNone(disposition.result_id)
        for job_id in (self.job.job_id, job.job_id):
            self.assertEqual(self.fixture.read(job_id)["acceptance"]["decision"], "unknown")

    def test_whole_multipart_result_accepts_reply_to_any_part_and_preserves_reason(self) -> None:
        self.deliver(multipart=True)
        before = self.state.get_provider_job(self.job.job_id)
        disposition, created = self.assess(self.request(reply=102))
        self.assertTrue(created)
        self.assertEqual(disposition.disposition, "applied")
        self.assertEqual(disposition.job_id, self.job.job_id)
        self.assertEqual(disposition.result_id, self.result.result_id)
        self.assertEqual(disposition.decision, "accepted")
        self.assertEqual(disposition.reason, "Проверено API и Файлы")
        self.assertEqual(disposition.revision, 1)
        self.assertIsNone(disposition.predecessor_id)
        self.assertEqual(self.state.get_provider_job(self.job.job_id), before)
        outcome = self.fixture.read(self.job.job_id)
        self.assertEqual(outcome["acceptance"]["decision"], "accepted")
        self.assertEqual(outcome["acceptance"]["source"], "owner")
        notices = self.state._connection.execute(
            "SELECT kind,assessment_disposition_id,reply_to_message_id FROM task_lifecycle_notices"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in notices], [("outcome_assessed", disposition.disposition_id, 50)]
        )

    def test_legacy_receipt_and_missing_part_cannot_establish_whole_result_authority(self) -> None:
        self.deliver(multipart=True)
        outbox = self.state.get_telegram_outbox_for_job(self.job.job_id)
        for number, update in (
            (50, "receipt_validation_version=0"),
            (51, "receipt_validation_version=1,telegram_message_id=NULL"),
        ):
            with self.state._connection:
                self.state._connection.execute(
                    f"UPDATE telegram_outbox_parts SET {update} WHERE outbox_id=? AND part_index=2",
                    (outbox.outbox_id,),
                )
            record, _ = self.assess(self.request(number))
            self.assertEqual(record.disposition, "refused")
            self.assertEqual(
                self.fixture.read(self.job.job_id)["acceptance"]["decision"], "unknown"
            )

    def test_refusal_is_immutable_after_receipts_arrive_and_duplicate_reuses_notice(self) -> None:
        request = self.request()
        refused, created = self.assess(request)
        self.assertTrue(created)
        self.assertEqual(refused.disposition, "refused")
        self.deliver()
        duplicate, created = self.assess(request)
        self.assertFalse(created)
        self.assertEqual(duplicate, refused)
        applied, created = self.assess(self.request(51))
        self.assertTrue(created)
        self.assertEqual(applied.disposition, "applied")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            2,
        )

    def test_correction_uses_latest_applied_owner_command_despite_unknown_ack(self) -> None:
        self.deliver()
        first, _ = self.assess(self.request())
        notice = self.state.task_notices.lease_notice(
            "example-sender", now=fixtures.datetime.now(fixtures.timezone.utc)
        )
        assert notice is not None and notice.lease_token is not None
        now = fixtures.datetime.now(fixtures.timezone.utc)
        self.state.task_notices.begin_send(notice.notice_id, notice.lease_token, now=now)
        self.state.task_notices.mark_send_unknown(
            notice.notice_id, notice.lease_token, error_code="example-network", now=now
        )
        correction, _ = self.assess(self.request(51, reply=50, text="/assess rework Исправить API"))
        self.assertEqual(correction.disposition, "applied")
        self.assertEqual(correction.predecessor_id, first.disposition_id)
        self.assertEqual(correction.revision, 2)
        for number, reply in ((52, 101), (53, 50)):
            stale, _ = self.assess(self.request(number, reply=reply))
            self.assertEqual(stale.disposition, "refused")
        latest = self.fixture.read(self.job.job_id)["acceptance"]
        self.assertEqual(latest["decision"], "rework")
        self.assertEqual(latest["revision"], 2)

    def test_changed_fingerprint_cannot_replace_recorded_command(self) -> None:
        self.deliver()
        request = self.request()
        first, _ = self.assess(request)
        for changed in (
            replace(request, text="/assess rework Изменённая причина"),
            replace(request, reply_message_id=999),
            replace(request, quote_text="Example quote"),
        ):
            with self.subTest(changed=changed), self.assertRaises(StateError):
                self.assess(changed)
        self.assertEqual(self.assess(request), (first, False))

    def test_historical_generation_is_assessed_without_retargeting_execution(self) -> None:
        self.deliver()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET status='archived',generation=generation+1 WHERE session_id=?",
                (self.job.session_id,),
            )
        before = self.state.get_provider_job(self.job.job_id)
        record, _ = self.assess(self.request())
        self.assertEqual(record.disposition, "applied")
        self.assertEqual(self.state.get_provider_job(self.job.job_id), before)

    def test_scope_actor_quote_material_and_syntax_refuse_without_provider_jobs(self) -> None:
        self.deliver()
        variants = (
            {"owner_user_id": 43},
            {"thread_id": 78},
            {"reply_message_id": None},
            {"quote_text": "Example quote"},
            {"text_source": "caption"},
            {"has_material": True},
            {"text": "/assess accepted"},
            {"text": "/assess invalid Example"},
        )
        for number, values in enumerate(variants, start=50):
            request = replace(self.request(number), **values)
            if values.get("owner_user_id") == 43:
                with self.assertRaises(StateError):
                    self.assess(request)
            else:
                record, _ = self.assess(request)
                self.assertEqual(record.disposition, "refused")
        self.assertEqual(
            self.state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0], 1
        )

    def test_notice_fault_rolls_back_disposition_input_receipt_and_eligibility(self) -> None:
        self.deliver()
        before = "\n".join(self.state._connection.iterdump())
        with patch.object(
            self.state.task_notices,
            "prepare_notice_in_transaction",
            side_effect=sqlite3.OperationalError("example-fault"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.assess(self.request())
        self.assertEqual("\n".join(self.state._connection.iterdump()), before)
        record, _ = self.assess(self.request())
        self.assertEqual(record.disposition, "applied")

    def test_decisions_and_refusals_cannot_be_updated_or_deleted(self) -> None:
        self.deliver()
        self.assess(self.request())
        for statement in (
            "UPDATE outcome_assessment_dispositions SET decision='rework'",
            "DELETE FROM outcome_assessment_dispositions",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.state._connection.execute(statement)

    def test_document_part_requires_its_own_validated_receipt(self) -> None:
        self.deliver(multipart=True)
        outbox = self.state.get_telegram_outbox_for_job(self.job.job_id)
        with self.state._connection:
            self.state._connection.execute(
                """UPDATE telegram_outbox_parts SET part_type='document',file_path='/home/example/spool/example.txt',
                   file_name='example.txt',file_size=4,file_sha256=?,receipt_validation_version=0
                   WHERE outbox_id=? AND part_index=2""",
                ("0" * 64, outbox.outbox_id),
            )
        refused, _ = self.assess(self.request())
        self.assertEqual(refused.disposition, "refused")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox_parts SET receipt_validation_version=1 WHERE outbox_id=? AND part_index=2",
                (outbox.outbox_id,),
            )
        record, _ = self.assess(self.request(51, reply=102))
        self.assertEqual(record.disposition, "applied")

    def test_nullable_revision_cannot_bypass_the_applied_schema_check(self) -> None:
        self.deliver()
        self.assess(self.request())
        record = dict(
            self.state._connection.execute(
                "SELECT * FROM outcome_assessment_dispositions"
            ).fetchone()
        )
        record.update(disposition_id="example-null-revision", input_message_id=51, revision=None)
        placeholders = ",".join("?" for _ in record)
        with self.assertRaises(sqlite3.IntegrityError):
            self.state._connection.execute(
                f"INSERT INTO outcome_assessment_dispositions VALUES ({placeholders})",
                tuple(record.values()),
            )
