"""Ingress precautions inherit task-notice certainty without changing execution."""

from __future__ import annotations

import unittest
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import StoredTurnOutcome, TurnResult
from hermes_codex_router.codex_ingress_notice import prepare_ingress_notice
from hermes_codex_router.codex_observed_result import ObservedTurnResults
from hermes_codex_router.codex_recovery import reconcile_codex_completion, recover_codex_job
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.task_notice_sender import deliver_task_notice
from tests import test_codex_ingress_live as fixtures
from tests.test_task_notice_sender import FakeTelegram
from tests.test_telegram_turn_provenance import row_values


class CodexIngressNoticeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexIngressLiveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.job = self.fixture.job

    def notice(self):
        row = self.state._connection.execute(
            "SELECT notice_id FROM task_lifecycle_notices WHERE kind='codex_ingress_control'"
        ).fetchone()
        self.assertIsNotNone(row)
        return self.state.task_notices.get_notice(row[0])

    def test_failed_preparation_preserves_cause_then_deduplicates_one_notice(self):
        with patch.object(
            self.state.task_notices,
            "prepare_notice_in_transaction",
            side_effect=RuntimeError("Example optional notice transaction failure"),
        ):
            self.assertTrue(self.fixture.control._poll_ingress(self.state))
        cause = row_values(self.state.codex_ingress_control.read_cause(self.job.job_id))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            0,
        )
        self.assertTrue(self.state.codex_ingress_control.prepare_notice(self.job.job_id))
        before = self.notice()
        self.assertFalse(self.state.codex_ingress_control.prepare_notice(self.job.job_id))
        self.assertEqual(self.notice(), before)
        self.assertEqual(
            row_values(self.state.codex_ingress_control.read_cause(self.job.job_id)), cause
        )
        self.assertEqual(self.fixture.client.calls.count("interrupt"), 1)

    def _uncertain_delivery(self, *, receipt_fault):
        self.assertTrue(self.fixture.control._poll_ingress(self.state))
        journal = ExecutionJournal(self.state)
        journal.record_completion(
            self.job.job_id, self.fixture.token, "Example raw completed output"
        )
        checkpoint = dict(self.fixture.checkpoint())
        target = row_values(self.state.codex_controls.read(self.job.job_id))
        job = self.state.get_provider_job(self.job.job_id)
        bot = FakeTelegram(error=None if receipt_fault else TimeoutError("Example unknown send"))
        if receipt_fault:
            with patch.object(
                self.state.task_notices,
                "complete_send",
                side_effect=RuntimeError("Example receipt commit failure"),
            ):
                result = deliver_task_notice(self.state.task_notices, bot, "example-notice-sender")
        else:
            result = deliver_task_notice(self.state.task_notices, bot, "example-notice-sender")
        self.assertIsNotNone(result.error)
        self.assertEqual(self.notice().status, "unknown")
        before = self.notice()
        prepare_ingress_notice(self.state, self.job.job_id)
        self.assertFalse(
            deliver_task_notice(self.state.task_notices, bot, "example-restarted-sender").worked
        )
        self.assertEqual(self.notice(), before)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(dict(self.fixture.checkpoint()), checkpoint)
        self.assertEqual(row_values(self.state.codex_controls.read(self.job.job_id)), target)
        self.assertEqual(self.state.get_provider_job(self.job.job_id), job)
        self.assertEqual(checkpoint["completed_text"], "Example raw completed output")

    def test_unknown_ingress_notice_is_not_resent_and_preserves_raw_completion(self):
        self._uncertain_delivery(receipt_fault=False)

    def test_receipt_commit_fault_is_unknown_and_preserves_execution(self):
        self._uncertain_delivery(receipt_fault=True)

    def test_no_cause_cannot_prepare_precaution_notice(self):
        with patch.object(self.state.codex_ingress_control, "transaction") as transaction:
            self.assertFalse(self.state.codex_ingress_control.prepare_notice(self.job.job_id))
        transaction.assert_not_called()
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            0,
        )

    def test_near_bound_saved_completion_omits_only_optional_explanation(self):
        self.assertTrue(self.fixture.control._poll_ingress(self.state))
        raw = "x" * 199_900
        journal = ExecutionJournal(self.state)
        journal.record_completion(self.job.job_id, self.fixture.token, raw)
        self.assertEqual(
            reconcile_codex_completion(
                self.state,
                self.fixture.fixture.harness.config,
                project_root=self.fixture.root(),
                job_id=self.job.job_id,
                lease_token=self.fixture.token,
                agent_id="codex",
                client_factory=lambda: self.fail("Saved completion must not invoke a provider"),
            ),
            "completed",
        )
        self.assertEqual(self.state.get_provider_result(self.job.job_id).visible_response, raw)
        self.assertEqual(self.fixture.checkpoint()["completed_text"], raw)

    def test_near_bound_observed_completion_omits_only_optional_explanation(self):
        self.assertTrue(self.fixture.control._poll_ingress(self.state))
        self.state.terminate_provider_job_with_notice(
            self.job.job_id,
            self.fixture.token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="example-observation-loss",
            sender_agent_id="codex",
            telegram_html="Example retained notice",
        )
        prefix = "Recovered completed Codex result:\n\n"
        raw = "x" * (200_000 - len(prefix))
        ObservedTurnResults(self.state, self.fixture.fixture.harness.config).apply_outcome(
            self.job.job_id,
            "example-thread",
            "example-turn",
            self.fixture.root(),
            StoredTurnOutcome("completed", TurnResult(raw, None, None)),
        )
        self.assertEqual(
            self.state.get_telegram_outbox_for_job(self.job.job_id).telegram_html, prefix + raw
        )
        self.assertEqual(self.state.get_provider_result(self.job.job_id).visible_response, raw)

    def test_stale_failure_notice_retains_ingress_explanation(self):
        self.assertTrue(self.fixture.control._poll_ingress(self.state))
        with patch.object(ExecutionJournal, "claim_stale", return_value=self.job):
            self.assertTrue(
                recover_codex_job(
                    self.state,
                    self.fixture.fixture.harness.config,
                    self.fixture.fixture.harness.service.registry,
                    "codex",
                    "example-recovery-worker",
                    lambda: cast(Any, fixtures.Client(StoredTurnOutcome("active"))),
                )
            )
        notice = self.state.get_telegram_outbox_for_job(self.job.job_id).telegram_html
        self.assertIn("Telegram ingress", notice)
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "indeterminate")
