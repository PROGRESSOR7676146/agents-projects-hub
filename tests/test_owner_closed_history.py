"""Owner closure releases historical blockers without rewriting uncertain evidence."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from hermes_codex_router.codex_server_service import require_idle_state
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState
from tests import test_codex_turn_controls as control_fixtures
from tests import test_outbox_sender as outbox_fixtures


class OwnerClosedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = control_fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.db = self.state._connection
        self.job_id = self.fixture.job_id
        self.path = self.fixture.fixture.config.state_path
        self.job = self.state.get_provider_job(self.job_id)

    def uncertain(self, *, terminal=None):
        self.fixture.journal.record_turn(self.job_id, self.fixture.token, "example-turn")
        self.state.terminate_provider_job_with_notice(
            self.job_id,
            self.fixture.token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="example_unknown",
            sender_agent_id="codex",
            telegram_html="Outcome remains unknown",
            terminal_turn_status=terminal,
        )

    def archive(self):
        # Fixture setup, not a runtime repair operation.
        with self.db:
            self.db.execute(
                "UPDATE agent_sessions SET status='archived' WHERE session_id=?",
                (self.job.session_id,),
            )

    def test_archived_resolved_history_passes_without_inventing_terminality(self):
        self.uncertain()
        self.archive()
        with self.assertRaises(ValueError):
            require_idle_state(self.path)
        self.state.resolve_indeterminate_job(self.job_id, "acknowledged")
        before = self.db.serialize()
        require_idle_state(self.path)
        self.assertEqual(self.db.serialize(), before)
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assertIsNone(
            self.db.execute("SELECT * FROM provider_turn_terminal_evidence").fetchone()
        )

    def test_resolution_of_current_session_alone_does_not_assert_idle(self):
        self.uncertain()
        self.state.resolve_indeterminate_job(self.job_id, "superseded")
        with self.assertRaises(ValueError):
            require_idle_state(self.path)

    def test_active_and_retry_work_still_blocks_after_archive(self):
        for status in ("queued", "retry_wait", "leased", "executing", "result_ready"):
            with self.subTest(status=status):
                with self.db:
                    self.db.execute(
                        "UPDATE provider_jobs SET status=?,lease_owner=?,lease_token=?,lease_expires_at=? WHERE job_id=?",
                        (
                            status,
                            self.job.lease_owner if status in {"leased", "executing"} else None,
                            self.job.lease_token if status in {"leased", "executing"} else None,
                            self.job.lease_expires_at
                            if status in {"leased", "executing"}
                            else None,
                            self.job_id,
                        ),
                    )
                self.archive()
                with self.assertRaises(ValueError):
                    require_idle_state(self.path)

    def test_exact_terminal_control_requires_owner_resolution_and_keeps_send_fence(self):
        with self.db:
            self.fixture.journal.record_turn(self.job_id, self.fixture.token, "example-turn")
            self.db.execute(
                """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
                   interrupt_source='protective',interrupt_outcome='unknown' WHERE job_id=?""",
                (datetime.now(timezone.utc).isoformat(), "example-sender", self.job_id),
            )
        self.state.terminate_provider_job_with_notice(
            self.job_id,
            self.fixture.token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="example_unknown",
            sender_agent_id="codex",
            telegram_html="Interrupted; partial effects unknown",
            terminal_turn_status="interrupted",
        )
        before_control = tuple(self.fixture.row())
        with self.assertRaises(ValueError):
            require_idle_state(self.path)
        self.assertIsNotNone(persistent_root_blocker(self.db, topic_id=self.job.topic_id))
        self.state.resolve_indeterminate_job(self.job_id, "acknowledged")
        require_idle_state(self.path)
        self.assertIsNone(persistent_root_blocker(self.db, topic_id=self.job.topic_id))
        self.assertEqual(self.state.effective_nonterminal_provider_job_counts(("codex",)), {})
        self.assertEqual(tuple(self.fixture.row()), before_control)
        self.assertIsNone(self.fixture.row()["owner_quiesced_at"])
        self.assertEqual(self.fixture.row()["interrupt_outcome"], "unknown")
        # A still-owned read remains a blocker even after administrative closure.
        with self.db:
            self.db.execute("""UPDATE codex_turn_controls SET read_claim_token='example-reader',
                read_claim_owner='example-worker',read_claim_expires_at='example-expiry'""")
        with self.assertRaises(ValueError):
            require_idle_state(self.path)
        self.assertIsNotNone(persistent_root_blocker(self.db, topic_id=self.job.topic_id))
        from hermes_codex_router.codex_turn_controls import CodexTurnControls

        CodexTurnControls(self.db, transaction=self.state._immediate_transaction).finish_late_read(
            self.job_id, "example-reader"
        )
        require_idle_state(self.path)
        peer = self.state.observe_topic(
            project_id="example-alias",
            chat_id=self.job.chat_id,
            thread_id=177,
            title="Example peer",
            execution_root=self.fixture.root,
        )
        session = self.state.activate_agent(peer.topic_id, "codex", "example-model", "high")
        next_job, _ = self.state.enqueue_provider_job(
            idempotency_key="example-after-owner-closure",
            chat_id=self.job.chat_id,
            message_id=177,
            topic_id=peer.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="New explicit owner work",
        )
        leased = self.state.lease_provider_job("codex", "example-worker")
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, next_job.job_id)
        self.assertEqual(tuple(self.fixture.row()), before_control)

    def test_other_native_identity_cannot_retire_control_or_establish_idle(self):
        self.fixture.journal.record_turn(self.job_id, self.fixture.token, "example-turn")
        with self.db:
            self.db.execute(
                """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
                   interrupt_source='protective' WHERE job_id=?""",
                (datetime.now(timezone.utc).isoformat(), "example-sender", self.job_id),
            )
        self.state.mark_provider_job_indeterminate(
            self.job_id, self.fixture.token, error_code="example_unknown"
        )
        self.state.resolve_indeterminate_job(self.job_id, "acknowledged")
        for thread, turn, root in (
            ("other-thread", "example-turn", str(self.fixture.root)),
            ("example-thread", "other-turn", str(self.fixture.root)),
            ("example-thread", "example-turn", "/home/example/other-project"),
        ):
            with self.subTest(thread=thread, turn=turn, root=root), self.db:
                self.db.execute("DELETE FROM provider_turn_terminal_evidence")
                self.db.execute(
                    """INSERT INTO provider_turn_terminal_evidence VALUES
                       (?,'interrupted',?,?,?,'example-time')""",
                    (self.job_id, thread, turn, root),
                )
            with self.assertRaises(ValueError):
                require_idle_state(self.path)
            self.assertIsNotNone(persistent_root_blocker(self.db, topic_id=self.job.topic_id))

    def test_owner_resolution_without_exact_terminal_proof_keeps_control_blocked(self):
        self.fixture.journal.record_turn(self.job_id, self.fixture.token, "example-turn")
        with self.db:
            self.db.execute(
                """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
                   interrupt_source='protective' WHERE job_id=?""",
                (datetime.now(timezone.utc).isoformat(), "example-sender", self.job_id),
            )
        self.state.mark_provider_job_indeterminate(
            self.job_id, self.fixture.token, error_code="example_unknown"
        )
        self.state.resolve_indeterminate_job(self.job_id, "acknowledged")
        self.archive()
        with self.assertRaises(ValueError):
            require_idle_state(self.path)
        self.assertIsNotNone(persistent_root_blocker(self.db, topic_id=self.job.topic_id))

    def test_exact_final_delivery_consent_releases_only_saved_delivery_wait(self):
        f = outbox_fixtures.TelegramOutboxSenderTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        job_id = f.ready_outbox("codex", 151)
        state = HubState.open(f.config.state_path, codex_permission_profile=None)
        self.addCleanup(state.close)
        job = state.get_provider_job(job_id)
        outbox = state.get_telegram_outbox_for_job(job_id)
        with state._connection:
            state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/project'"
            )
            state._connection.execute("UPDATE telegram_outbox SET status='unknown'")
        with self.assertRaises(ValueError):
            require_idle_state(f.config.state_path)
        preview = state.preview_delivery_control("final_outbox", outbox.outbox_id)
        state.reconcile_delivery_control(
            "final_outbox",
            outbox.outbox_id,
            expected_snapshot=preview.snapshot,
            accept_unconfirmed_delivery=True,
        )
        before = state._connection.serialize()
        require_idle_state(f.config.state_path)
        self.assertEqual(state._connection.serialize(), before)
        self.assertEqual(state.get_provider_job(job_id), job)
        self.assertEqual(state.get_telegram_outbox_for_job(job_id).status, "unknown")


if __name__ == "__main__":
    unittest.main()
