"""Explicit local consent reconciles delivery waits, never execution or receipts."""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.state import StateError
from tests import test_delivery_control_preview as preview_fixtures


class DeliveryControlAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = preview_fixtures.DeliveryControlPreviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.db = self.state._connection
        self.job = self.fixture.job
        self.outbox = self.fixture.outbox

    def apply(self, kind="final_outbox", target=None, token=None):
        target = target or self.outbox.outbox_id
        token = token or self.state.preview_delivery_control(kind, target).snapshot
        return self.state.reconcile_delivery_control(
            kind,
            target,
            expected_snapshot=token,
            accept_unconfirmed_delivery=True,
        )

    def effective(self, kind="final_outbox", target=None) -> bool:
        from hermes_codex_router.delivery_control_predicates import (
            final_control_reconciled,
            progress_control_reconciled,
        )

        table, key, predicate = (
            ("telegram_outbox", "outbox_id", final_control_reconciled)
            if kind == "final_outbox"
            else ("provider_progress_deliveries", "progress_id", progress_control_reconciled)
        )
        return bool(
            self.db.execute(
                f"SELECT {predicate('target')} FROM {table} target WHERE {key}=?",
                (target or self.outbox.outbox_id,),
            ).fetchone()[0]
        )

    def enqueue_tail(self):
        session = self.state.get_session(self.job.session_id)
        tail, _ = self.state.enqueue_provider_job(
            idempotency_key="example-control-tail",
            chat_id=self.job.chat_id,
            message_id=153,
            topic_id=self.job.topic_id,
            agent_id=self.job.agent_id,
            session_id=self.job.session_id,
            session_generation=self.job.session_generation,
            provider_session_id=self.job.provider_session_id,
            model=session.model,
            effort=session.effort,
            payload_text="Example successor",
            context_watermark=None,
            handoff_id=None,
        )
        return tail

    def test_apply_inserts_only_immutable_consent_and_never_sends_or_completes(self) -> None:
        tables = (
            "provider_jobs",
            "provider_job_results",
            "telegram_outbox",
            "telegram_outbox_parts",
            "provider_execution_checkpoints",
            "provider_job_holds",
            "provider_stop_requests",
            "agent_sessions",
            "topics",
            "provider_visible_items",
            "provider_progress_deliveries",
        )
        before = {table: self.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        token = self.fixture.preview().snapshot
        decision = self.apply(token=token)
        self.assertEqual(decision.control_effect, "delivery_wait_reconciled")
        self.assertEqual(self.apply(token=token), decision)
        self.assertTrue(self.effective())
        for table in tables:
            self.assertEqual(self.db.execute(f"SELECT * FROM {table}").fetchall(), before[table])
        self.assertIsNone(self.state.lease_telegram_outbox("opencode", "example-sender"))
        for command in (
            "DELETE FROM telegram_delivery_control_dispositions",
            "UPDATE telegram_delivery_control_dispositions SET snapshot='invalid'",
        ):
            with self.assertRaises(sqlite3.IntegrityError), self.db:
                self.db.execute(command)

    def test_exact_consent_token_cas_and_nested_transaction_refuse(self) -> None:
        token = self.fixture.preview().snapshot
        for consent in (False, None, 1, "yes"):
            with self.subTest(consent=consent), self.assertRaises(StateError):
                self.state.reconcile_delivery_control(
                    "final_outbox",
                    self.outbox.outbox_id,
                    expected_snapshot=token,
                    accept_unconfirmed_delivery=consent,  # type: ignore[arg-type]
                )
        with self.db:
            self.db.execute("UPDATE telegram_outbox_parts SET telegram_html='Changed'")
        with self.assertRaises(StateError):
            self.apply(token=token)
        self.db.execute("BEGIN")
        try:
            with self.assertRaises(StateError):
                self.apply(token=token)
            self.assertTrue(self.db.in_transaction)
        finally:
            self.db.rollback()
        current = self.fixture.preview().snapshot
        self.apply(token=current)
        with self.assertRaises(StateError):
            self.apply(token=token)

    def test_session_and_scope_changes_preserve_original_consent_and_exact_retry(self) -> None:
        token = self.fixture.preview().snapshot
        original = self.apply(token=token)
        replacement = self.state.new_active_session(
            self.job.topic_id, expected_session_id=self.job.session_id
        )
        self.assertNotEqual(replacement.session_id, self.job.session_id)
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        self.assertTrue(self.effective())
        self.assertEqual(self.apply(token=token), original)
        self.assertFalse(self.state.topic_has_pending_provider_job(self.job.topic_id))

    def test_progress_and_final_on_same_job_each_require_consent(self) -> None:
        first = self.fixture.progress()
        second = self.fixture.progress("example-progress-2", "example-item-2")
        self.apply("progress_delivery", first)
        self.assertTrue(self.effective("progress_delivery", first))
        self.assertFalse(self.effective("progress_delivery", second))
        self.assertFalse(self.effective())
        self.assertTrue(self.state.topic_has_pending_provider_job(self.job.topic_id))
        self.apply()
        self.assertFalse(self.state.topic_has_pending_provider_job(self.job.topic_id))
        self.assertFalse(self.effective("progress_delivery", second))
        self.apply("progress_delivery", second)
        self.assertTrue(self.effective("progress_delivery", second))

    def test_progress_consent_survives_later_result_metadata(self) -> None:
        progress = self.fixture.progress()
        token = self.state.preview_delivery_control("progress_delivery", progress).snapshot
        original = self.apply("progress_delivery", progress, token)
        with self.db:
            self.db.execute("UPDATE provider_job_results SET visible_response='Later result'")
            self.db.execute("UPDATE agent_sessions SET model='other-model',status='archived'")
        self.assertTrue(self.effective("progress_delivery", progress))
        self.assertEqual(self.apply("progress_delivery", progress, token), original)

    def test_destination_or_project_drift_invalidates_effect_without_replacing_decision(
        self,
    ) -> None:
        token = self.fixture.preview().snapshot
        original = self.apply(token=token)
        with self.db:
            self.db.execute("UPDATE topics SET project_id='other-example-project'")
        repeated = self.apply(token=token)
        self.assertEqual(repeated.disposition_id, original.disposition_id)
        self.assertEqual(repeated.control_effect, "disposition_binding_changed")
        self.assertFalse(self.effective())
        self.assertTrue(self.state.topic_has_pending_provider_job(self.job.topic_id))

    def test_full_consent_can_cover_schema44_scope_freeze_but_never_destination_change(
        self,
    ) -> None:
        old = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        self.apply()
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        self.assertTrue(self.effective())
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE topics SET thread_id=999")

    def test_real_final_exhaustion_stays_failed_and_requires_consent_for_adoption(self) -> None:
        from hermes_codex_router.session_adoption_state import CodexSessionOrigins

        with self.db:
            self.db.execute("UPDATE telegram_outbox SET status='pending',attempt_count=19")
        leased = self.state.lease_telegram_outbox("opencode", "example-sender")
        assert leased is not None and leased.lease_token is not None
        self.state.retry_telegram_outbox(
            leased.outbox_id, leased.lease_token, error_code="example_rejection", delay_seconds=0
        )
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "failed")
        adoption = CodexSessionOrigins(self.state)
        with self.assertRaisesRegex(StateError, "pending_delivery"):
            adoption._topic_idle(self.job.topic_id)
        self.apply()
        adoption._topic_idle(self.job.topic_id)
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id).status, "failed")

    def test_failed19_and21_and_every_sender_lease_field_refuse(self) -> None:
        for count in (19, 21):
            # 21 is deliberately corrupt legacy state; current DDL caps attempts at 20.
            self.db.execute("PRAGMA ignore_check_constraints=ON")
            with self.db:
                self.db.execute(
                    "UPDATE telegram_outbox SET status='failed',attempt_count=?", (count,)
                )
            self.db.execute("PRAGMA ignore_check_constraints=OFF")
            with self.subTest(count=count), self.assertRaises(StateError):
                self.apply(token="a" * 64)
        for field in ("lease_owner", "lease_token", "lease_expires_at"):
            # Supported writes cannot park a leased target; inject corrupt legacy state.
            self.db.execute("PRAGMA ignore_check_constraints=ON")
            with self.db:
                self.db.execute("UPDATE telegram_outbox SET status='unknown',attempt_count=20")
                self.db.execute(f"UPDATE telegram_outbox SET {field}='example-lease'")
            self.db.execute("PRAGMA ignore_check_constraints=OFF")
            with self.subTest(field=field), self.assertRaises(StateError):
                self.apply(token="a" * 64)
            with self.db:
                self.db.execute(f"UPDATE telegram_outbox SET {field}=NULL")

    def test_apply_fault_rolls_back_and_original_preview_is_still_retryable(self) -> None:
        token = self.fixture.preview().snapshot
        with self.db:
            self.db.execute("""CREATE TRIGGER example_fault AFTER INSERT ON
                telegram_delivery_control_dispositions BEGIN
                SELECT RAISE(ABORT,'example fault'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(token=token)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM telegram_delivery_control_dispositions"
            ).fetchone()[0],
            0,
        )
        self.assertTrue(self.state.topic_has_pending_provider_job(self.job.topic_id))
        with self.db:
            self.db.execute("DROP TRIGGER example_fault")
        self.apply(token=token)

    def test_raw_counts_remain_while_effective_delivery_barriers_clear(self) -> None:
        from hermes_codex_router.reliability_alerts import evaluate_reliability_alerts

        progress = self.fixture.progress()
        self.assertEqual(
            len(evaluate_reliability_alerts(self.state.delivery.uncertain_counts())), 2
        )
        self.apply()
        self.apply("progress_delivery", progress)
        counts = self.state.delivery.uncertain_counts()
        self.assertEqual(counts["unknown_delivery"], 1)
        self.assertEqual(counts["unknown_progress_delivery"], 1)
        self.assertEqual(counts["reconciled_final_controls"], 1)
        self.assertEqual(counts["reconciled_progress_controls"], 1)
        self.assertEqual(counts["outstanding_delivery_holds"], 0)
        self.assertEqual(evaluate_reliability_alerts(counts), ())
        self.assertEqual(self.state.provider_chat_activities(("opencode",)), ())
        delivery = self.state.provider_job_outcome(self.job.job_id).as_dict()["result_delivery"]
        self.assertEqual(delivery["delivery_control"], "delivery_wait_reconciled")
        self.assertEqual(delivery["status"], "unknown")
        self.assertFalse(delivery["receipts_complete"])

    def test_status_uses_one_projection_when_other_connection_applies(self) -> None:
        from contextlib import closing

        from hermes_codex_router.controller_commands import ControllerCommandOrchestrator
        from hermes_codex_router.state import HubState

        token = self.fixture.preview().snapshot
        delivery = self.state.delivery
        projection = delivery.topic_delivery_controls
        calls = []

        def read_then_apply(topic_id):
            snapshot = projection(topic_id)
            calls.append(snapshot)
            with closing(HubState.open_existing(self.fixture.fixture.config.state_path)) as owner:
                owner.reconcile_delivery_control(
                    "final_outbox",
                    self.outbox.outbox_id,
                    expected_snapshot=token,
                    accept_unconfirmed_delivery=True,
                )
            return snapshot

        # The obsolete two-read path would observe old holds, then new full
        # control counts. The supported path must not call it at all.
        old_holds = delivery.topic_delivery_holds

        def old_read_then_apply(topic_id):
            snapshot = old_holds(topic_id)
            self.apply(token=token)
            return snapshot

        with (
            patch.object(delivery, "topic_delivery_controls", side_effect=read_then_apply),
            patch.object(
                delivery, "topic_delivery_holds", side_effect=old_read_then_apply
            ) as legacy,
        ):
            command = ControllerCommandOrchestrator(self.fixture.fixture.config, self.state)
            result = command.status(self.state.get_topic(self.job.topic_id), None)
        legacy.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertIn("Unknown Telegram delivery hold(s): 1", result.text)
        self.assertNotIn("reconciled", result.text)
        self.assertNotIn("-1", result.text)
        current = projection(self.job.topic_id)
        self.assertEqual(current["outstanding"], 0)
        self.assertEqual(current["released"], 1)
        self.assertEqual(current["queue_only"], 0)

    def test_schema44_preview_and_retry_show_separate_full_control_after_scope_change(self) -> None:
        old = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        self.apply()
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        preview = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.assertEqual(preview.hold_status, "disposition_binding_changed")
        self.assertEqual(preview.control_effect, "delivery_wait_reconciled")
        self.assertNotIn(
            "topic_binding_retained_for_disposition_lifetime", preview.control_consequences
        )
        self.assertNotIn(
            "topic_new_model_agent_local_return_remain_blocked", preview.control_consequences
        )
        retry = self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        self.assertEqual(retry.hold_status, "disposition_binding_changed")
        self.assertEqual(retry.control_effect, "delivery_wait_reconciled")
        for extra in (
            [],
            ["--apply", "--snapshot", old.snapshot, "--continue-without-confirmed-delivery"],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "delivery-hold",
                            str(self.fixture.fixture.config.state_path),
                            self.outbox.outbox_id,
                            *extra,
                        ]
                    ),
                    0,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result["control_effect"], "delivery_wait_reconciled")
            self.assertNotIn("hold remains", result["effect"])

    def test_local_cli_apply_requires_exact_flags_and_preserves_unknown(self) -> None:
        token = self.fixture.preview().snapshot
        arguments = [
            "delivery-control",
            str(self.fixture.fixture.config.state_path),
            "final_outbox",
            self.outbox.outbox_id,
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main([*arguments, "--apply", "--snapshot", token, "--accept-unconfirmed-delivery"]),
                0,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["control_effect"], "delivery_wait_reconciled")
        self.assertFalse(result["productive_replay_authorized"])
        self.assertFalse(result["automatic_resend"])
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id).status, "unknown")

    def test_two_schema44_holds_each_need_full_consent_before_scope_move(self) -> None:
        first = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=first.snapshot,
            continue_without_confirmed_delivery=True,
        )
        tail = self.enqueue_tail()
        lease = self.state.lease_provider_job("opencode", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(tail.job_id, lease.lease_token)
        self.state.commit_provider_result(
            tail.job_id,
            lease.lease_token,
            visible_response="Example next result",
            sender_agent_id="opencode",
            telegram_html="Example next result",
        )
        second = self.state.get_telegram_outbox_for_job(tail.job_id)
        with self.db:
            self.db.execute(
                "UPDATE telegram_outbox SET status='unknown' WHERE outbox_id=?", (second.outbox_id,)
            )
        preview = self.state.preview_delivery_hold(second.outbox_id)
        self.state.release_delivery_hold(
            second.outbox_id,
            expected_snapshot=preview.snapshot,
            continue_without_confirmed_delivery=True,
        )
        self.apply()
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        self.apply(target=second.outbox_id)
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        self.assertTrue(self.effective())
        self.assertTrue(self.effective(target=second.outbox_id))

    def test_adoption_relocation_lane_and_drain_need_both_delivery_targets(self) -> None:
        from hermes_codex_router.project_editing import ProjectEditStore
        from hermes_codex_router.session_adoption_state import CodexSessionOrigins
        from hermes_codex_router.state_lanes import LaneState

        progress = self.fixture.progress()
        adoption = CodexSessionOrigins(self.state)
        editing = ProjectEditStore(self.state, self.fixture.fixture.config.registry_path)
        lane = LaneState(self.db)
        with self.assertRaises(StateError):
            adoption._topic_idle(self.job.topic_id)
        with self.assertRaises(StateError):
            editing._assert_relocation_idle("example-project")
        with self.assertRaises(StateError):
            lane._require_topic_execution_idle_locked(self.job.topic_id)
        self.apply()
        with self.assertRaisesRegex(StateError, "pending_delivery"):
            adoption._topic_idle(self.job.topic_id)
        with self.assertRaisesRegex(StateError, "pending_delivery"):
            editing._assert_relocation_idle("example-project")
        self.apply("progress_delivery", progress)
        adoption._topic_idle(self.job.topic_id)
        editing._assert_relocation_idle("example-project")
        lane._require_topic_execution_idle_locked(self.job.topic_id)
        lane._require_execution_scope_idle_locked("root:/home/example/project")
        self.assertEqual(self.state.nonterminal_provider_job_counts(("opencode",)), {"opencode": 1})
        self.assertEqual(self.state.effective_nonterminal_provider_job_counts(("opencode",)), {})
        self.state.set_writer_mode(self.job.session_id, "local")
        with self.assertRaisesRegex(StateError, "local_writer"):
            adoption._topic_idle(self.job.topic_id)
        self.assertTrue(self.effective())

    def test_fifo_execution_and_sender_continue_without_resending_reconciled_head(self) -> None:
        from hermes_codex_router.outbox_sender import TelegramOutboxSender
        from tests.test_outbox_sender import Bot

        tail = self.enqueue_tail()
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        self.apply()
        lease = self.state.lease_provider_job("opencode", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.assertEqual(lease.job_id, tail.job_id)
        self.state.mark_provider_job_executing(tail.job_id, lease.lease_token)
        self.state.commit_provider_result(
            tail.job_id,
            lease.lease_token,
            visible_response="Example tail result",
            sender_agent_id="opencode",
            telegram_html="Example tail result",
        )
        bot = Bot()
        with_sender = TelegramOutboxSender(
            self.fixture.fixture.config, telegram_bots={"opencode": bot, "antigravity": Bot()}
        )
        self.addCleanup(with_sender.close)
        self.assertTrue(with_sender._deliver_one("opencode"))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(self.state.get_telegram_outbox_for_job(tail.job_id).status, "delivered")
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id).status, "unknown")
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "result_ready")

    def test_embedded_sender_observes_same_target_consent_without_resending_head(self) -> None:
        from types import SimpleNamespace
        from typing import Any, cast

        from hermes_codex_router.service import ProjectHubService
        from tests.test_outbox_sender import Bot

        self.apply()
        tail = self.enqueue_tail()
        lease = self.state.lease_provider_job("opencode", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(tail.job_id, lease.lease_token)
        self.state.commit_provider_result(
            tail.job_id,
            lease.lease_token,
            visible_response="Example embedded result",
            sender_agent_id="opencode",
            telegram_html="Example embedded result",
        )
        bot = Bot()
        service = cast(Any, object.__new__(ProjectHubService))
        service.config = self.fixture.fixture.config
        service.external_services = {"opencode": SimpleNamespace(telegram=bot)}
        self.assertTrue(service._deliver_embedded_outbox(self.state, "opencode"))
        self.assertFalse(service._deliver_embedded_outbox(self.state, "opencode"))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id).status, "unknown")

    def test_model_and_provider_switches_preserve_historical_consent(self) -> None:
        token = self.fixture.preview().snapshot
        original = self.apply(token=token)
        self.state.activate_agent(self.job.topic_id, "antigravity", "example-model", "low")
        self.state.activate_agent(self.job.topic_id, "opencode", "example-model-2", "medium")
        self.assertTrue(self.effective())
        self.assertEqual(self.apply(token=token), original)

    def test_full_consent_never_resolves_native_uncertainty_or_owner_holds(self) -> None:
        from hermes_codex_router.session_adoption_state import CodexSessionOrigins

        tail = self.enqueue_tail()
        with self.db:
            self.db.execute("DELETE FROM provider_job_results")
            self.db.execute(
                "UPDATE provider_jobs SET status='indeterminate' WHERE job_id=?", (self.job.job_id,)
            )
        self.apply()
        self.assertTrue(self.effective())
        with self.assertRaises(StateError):
            CodexSessionOrigins(self.state)._topic_idle(self.job.topic_id)
        # An exact already-admitted input remains an idempotent lookup; a new
        # request still encounters the independent root blocker.
        with self.assertRaisesRegex(StateError, "persistent local writer or uncertainty"):
            self.state.enqueue_provider_job(
                idempotency_key="example-new-request",
                chat_id=self.job.chat_id,
                message_id=155,
                topic_id=self.job.topic_id,
                agent_id=self.job.agent_id,
                session_id=self.job.session_id,
                session_generation=self.job.session_generation,
                provider_session_id=None,
                model=self.job.model,
                effort=self.job.effort,
                payload_text="Example new request",
                context_watermark=None,
                handoff_id=None,
            )
        with self.db:
            self.db.execute(
                "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at) VALUES(?,?,'example-time')",
                (tail.job_id, self.job.job_id),
            )
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        self.assertEqual(self.state.get_provider_job(tail.job_id).status, "queued")

    def test_failed_native_proof_is_retained_and_mutation_invalidates_effect(self) -> None:
        self.fixture.progress()
        with self.db:
            self.db.execute("DELETE FROM provider_job_results")
            self.db.execute("UPDATE provider_jobs SET status='indeterminate'")
            self.db.execute("UPDATE telegram_outbox SET status='failed',attempt_count=20")
            self.db.execute(
                "UPDATE provider_execution_checkpoints SET provider_turn_id='example-turn'"
            )
            self.db.execute(
                """INSERT INTO provider_turn_terminal_evidence
                (job_id,terminal_status,provider_thread_id,provider_turn_id,project_root,observed_at)
                VALUES (?,'completed','example-thread','example-turn','/home/example/project','example-time')""",
                (self.job.job_id,),
            )
        token = self.fixture.preview().snapshot
        decision = self.apply(token=token)
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("DELETE FROM provider_turn_terminal_evidence")
        with self.db:
            self.db.execute(
                "UPDATE provider_turn_terminal_evidence SET provider_turn_id='wrong-turn'"
            )
        self.assertFalse(self.effective())
        retry = self.apply(token=token)
        self.assertEqual(retry.disposition_id, decision.disposition_id)
        self.assertEqual(retry.control_effect, "disposition_binding_changed")

    def test_unsupported_final_job_drift_invalidates_runtime_and_scope_coverage(self) -> None:
        old = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        token = self.fixture.preview().snapshot
        self.apply(token=token)
        for status in ("completed", "queued", "executing"):
            with self.subTest(status=status):
                lease = "example-lease" if status == "executing" else None
                with self.db:
                    self.db.execute(
                        "UPDATE provider_jobs SET status=?,lease_owner=?,lease_token=?,lease_expires_at=?",
                        (status, lease, lease, lease),
                    )
                self.assertFalse(self.effective())
                self.assertEqual(
                    self.apply(token=token).control_effect, "disposition_binding_changed"
                )
                with self.assertRaises(sqlite3.IntegrityError), self.db:
                    self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")

    def test_resultless_final_cannot_cover_later_result_ready_without_result(self) -> None:
        progress = self.fixture.progress()
        with self.db:
            self.db.execute("DELETE FROM provider_job_results")
            self.db.execute("UPDATE provider_jobs SET status='failed'")
        old = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        token = self.fixture.preview().snapshot
        self.apply(token=token)
        self.apply("progress_delivery", progress)
        with self.db:
            self.db.execute("UPDATE provider_jobs SET status='result_ready'")
        self.assertFalse(self.effective())
        self.assertEqual(self.apply(token=token).control_effect, "disposition_binding_changed")
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/other'")
        self.assertTrue(self.effective("progress_delivery", progress))
        self.assertTrue(self.state.topic_has_pending_provider_job(self.job.topic_id))

    def test_competing_owner_connections_return_one_immutable_decision(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import closing
        from threading import Barrier

        from hermes_codex_router.state import HubState

        token = self.fixture.preview().snapshot
        ready = Barrier(2)

        def consent():
            with closing(HubState.open_existing(self.fixture.fixture.config.state_path)) as owner:
                ready.wait(timeout=5)
                return owner.reconcile_delivery_control(
                    "final_outbox",
                    self.outbox.outbox_id,
                    expected_snapshot=token,
                    accept_unconfirmed_delivery=True,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(consent), pool.submit(consent)
            self.assertEqual(first.result(timeout=10), second.result(timeout=10))
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM telegram_delivery_control_dispositions"
            ).fetchone()[0],
            1,
        )

    def test_failed_full_count_does_not_subtract_unrelated_queue_only_unknown(self) -> None:
        first = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=first.snapshot,
            continue_without_confirmed_delivery=True,
        )
        other = self.enqueue_tail()
        lease = self.state.lease_provider_job("opencode", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(other.job_id, lease.lease_token)
        self.state.commit_provider_result(
            other.job_id,
            lease.lease_token,
            visible_response="Example result",
            sender_agent_id="opencode",
            telegram_html="Example result",
        )
        other_box = self.state.get_telegram_outbox_for_job(other.job_id)
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/project'")
            # Model the terminal delivery state, independently of provider attempts.
            self.db.execute(
                "UPDATE telegram_outbox SET status='failed',attempt_count=20 WHERE outbox_id=?",
                (other_box.outbox_id,),
            )
        self.apply(target=other_box.outbox_id)
        snapshot = self.state.delivery.topic_delivery_controls(self.job.topic_id)
        self.assertEqual(snapshot["queue_only"], 1)
        self.assertEqual(snapshot["unknown_final_reconciled"], 0)


if __name__ == "__main__":
    unittest.main()
