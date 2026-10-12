"""Late stop never replays productive work and shares the permanent send fence."""

from __future__ import annotations

import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

from hermes_codex_router.codex_appserver import CodexAppServerClient, StoredTurnOutcome, TurnResult
from hermes_codex_router.codex_late_control import CodexControlMaintenance, run_late_control_once
from hermes_codex_router.codex_recovery import recover_codex_job
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState
from hermes_codex_router.turn_observation import TurnObservation
from tests import test_codex_turn_controls as fixtures


class Client:
    def __init__(self, *outcomes, interrupt_error=None):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.interrupt_error = interrupt_error

    def read_turn_outcome(self, **kwargs):
        self.calls.append(("read", kwargs))
        return next(self.outcomes)

    def interrupt_turn(self, **kwargs):
        self.calls.append(("interrupt", kwargs))
        if self.interrupt_error is not None:
            raise self.interrupt_error

    def close(self):
        self.calls.append(("close", {}))


class LateCodexControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.job_id = self.fixture.job_id
        self.fixture.journal.record_turn(self.job_id, self.fixture.token, "example-turn")
        self.state.terminate_provider_job_with_notice(
            self.job_id,
            self.fixture.token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="example-stream-lost",
            sender_agent_id="codex",
            telegram_html="Example saved failure notice",
        )
        job = self.state.get_provider_job(self.job_id)
        self.stop_id = self.state.request_emergency_stop(
            topic_id=job.topic_id,
            chat_id=job.chat_id,
            message_id=901,
            target_agent_id="codex",
        )[0]
        self.now = datetime.now(timezone.utc)

    def row(self):
        row = self.state.codex_controls.read(self.job_id)
        assert row is not None
        return row

    def test_not_due_poll_does_not_take_write_lock_and_later_claim_is_single(self):
        self.assertTrue(
            self.run_once(Client(StoredTurnOutcome("active"), StoredTurnOutcome("active")))
        )
        statements = []
        self.state._connection.set_trace_callback(statements.append)
        try:
            self.assertIsNone(
                self.state.codex_controls.claim_late_read("example-idle-worker", now=self.now)
            )
        finally:
            self.state._connection.set_trace_callback(None)
        self.assertFalse(any(sql.upper().startswith("BEGIN") for sql in statements))
        self.now += timedelta(seconds=31)
        claimed = self.state.codex_controls.claim_late_read("example-next-worker", now=self.now)
        assert claimed is not None
        self.assertEqual(claimed["late_read_attempts"], 2)
        self.assertIsNone(
            self.state.codex_controls.claim_late_read("example-competing-worker", now=self.now)
        )

    def test_maintenance_shutdown_preserves_inflight_matched_reply(self):
        client = Client(StoredTurnOutcome("active"), StoredTurnOutcome("interrupted"))
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        stop = threading.Event()
        original_close = client.close

        def close():
            original_close()
            closed.set()

        def interrupt(**kwargs):
            client.calls.append(("interrupt", kwargs))
            entered.set()
            release.wait(2)
            if closed.is_set():
                raise EOFError("Example shutdown lost matched reply")

        client.close = close
        client.interrupt_turn = interrupt
        maintenance = CodexControlMaintenance(
            self.fixture.fixture.config,
            worker_id="example-maintenance",
            agent_id="codex",
            client_factory=lambda deadline: client,
            stop=stop,
        )
        thread = threading.Thread(target=maintenance.run_forever)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            stop.set()
            maintenance.close_client()
            self.assertFalse(closed.wait(0.05))
        finally:
            stop.set()
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.row()["interrupt_outcome"], "matched_ack")
        self.assertIsNotNone(self.row()["owner_quiesced_at"])
        self.assertEqual([call[0] for call in client.calls].count("interrupt"), 1)

    def run_once(self, client, **changes):
        return run_late_control_once(
            self.state,
            self.fixture.fixture.config,
            worker_id="example-late-worker",
            agent_id="codex",
            client_factory=lambda deadline: client,
            now=self.now,
            **changes,
        )

    def test_active_after_ack_retains_root_and_later_cycle_reads_without_second_send(self):
        first = Client(StoredTurnOutcome("active"), StoredTurnOutcome("active"))
        self.assertTrue(self.run_once(first))
        self.assertEqual([call[0] for call in first.calls], ["read", "interrupt", "read", "close"])
        row = self.row()
        self.assertEqual(row["interrupt_outcome"], "matched_ack")
        self.assertIsNotNone(row["owner_quiesced_at"])
        with self.state._immediate_transaction():
            self.assertIsNotNone(
                persistent_root_blocker(self.state._connection, topic_id=row["topic_id"])
            )
        self.assertFalse(self.run_once(Client()))
        self.now += timedelta(seconds=31)
        second = Client(StoredTurnOutcome("active"))
        self.assertTrue(self.run_once(second))
        self.assertEqual([call[0] for call in second.calls], ["read", "close"])

    def test_ambiguous_send_then_terminal_proof_never_clears_sender_owner(self):
        client = Client(
            StoredTurnOutcome("active"),
            StoredTurnOutcome("interrupted"),
            interrupt_error=EOFError("Example lost interrupt response"),
        )
        self.assertTrue(self.run_once(client))
        row = self.row()
        self.assertEqual(row["interrupt_outcome"], "unknown")
        self.assertIsNone(row["owner_quiesced_at"])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT terminal_status FROM provider_turn_terminal_evidence WHERE job_id=?",
                (self.job_id,),
            ).fetchone()[0],
            "interrupted",
        )
        with self.state._immediate_transaction():
            blocker = persistent_root_blocker(self.state._connection, topic_id=row["topic_id"])
            assert blocker is not None
            self.assertEqual(
                blocker.kind,
                "control",
            )
        self.now += timedelta(seconds=31)
        self.assertFalse(self.run_once(Client()))

    def test_already_completed_race_saves_exact_result_without_interrupt(self):
        client = Client(
            StoredTurnOutcome("completed", TurnResult("Example saved final", None, None))
        )
        self.assertTrue(self.run_once(client))
        self.assertEqual([call[0] for call in client.calls], ["read", "close"])
        # Covering stop withholds display, but terminal evidence is retained.
        self.assertEqual(
            self.state._connection.execute(
                "SELECT terminal_status FROM provider_turn_terminal_evidence WHERE job_id=?",
                (self.job_id,),
            ).fetchone()[0],
            "completed",
        )
        self.assertIsNone(self.row()["send_started_at"])
        saved = self.fixture.journal.read(self.job_id)
        assert saved is not None
        self.assertEqual(saved["completed_text"], "Example saved final")
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assertEqual(
            self.state._connection.execute("SELECT COUNT(*) FROM provider_job_results").fetchone()[
                0
            ],
            0,
        )
        notice = self.state.get_telegram_outbox_for_job(self.job_id)
        assert notice is not None
        self.assertNotIn("Example saved final", notice.telegram_html)

    def test_failed_connections_consume_three_cycles_before_network_and_never_reset(self):
        calls = []

        def unavailable(deadline):
            calls.append(deadline)
            self.assertEqual(self.row()["late_read_attempts"], len(calls))
            raise OSError("Example owning socket unavailable")

        for attempt in range(4):
            worked = run_late_control_once(
                self.state,
                self.fixture.fixture.config,
                worker_id="example-late-worker",
                agent_id="codex",
                client_factory=unavailable,
                now=self.now,
            )
            self.assertEqual(worked, attempt < 3)
            self.now += timedelta(seconds=31)
        self.assertEqual(len(calls), 3)
        self.assertIsNone(self.row()["send_started_at"])

    def test_alias_satellite_completion_preserves_sender_result_and_context_identity(self):
        config = self.fixture.fixture.config
        path = config.state_path.with_name("example-alias-state.db")
        alias = "example-codex-alias"
        codex_agent = next(agent for agent in config.agents if agent.runtime == "codex")
        config = replace(
            config, state_path=path, agents=(*config.agents, replace(codex_agent, agent_id=alias))
        )
        with closing(HubState.open(path, codex_permission_profile=None)) as state:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=78,
                title="Example alias",
                execution_root=self.fixture.root,
            )
            session = state.activate_agent(topic.topic_id, alias, "example-model", "high")
            state.activate_agent(topic.topic_id, "example-main", "example-other-model", "high")
            self.assertEqual(state.get_session(session.session_id).status, "satellite")
            watermark = state.record_visible_turn(
                topic.topic_id,
                agent_id="example-main",
                provider="example-provider",
                model="example-model",
                user_excerpt="Example context request",
                response_excerpt="Example visible context",
            )
            job, _ = state.enqueue_provider_job(
                idempotency_key="example-alias-input",
                chat_id=topic.chat_id,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id=alias,
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example alias task",
                context_watermark=watermark,
            )
            lease = state.lease_provider_job(alias, "example-alias-worker")
            assert lease is not None and lease.lease_token is not None
            token = lease.lease_token
            state.mark_provider_job_executing(job.job_id, token)
            journal = ExecutionJournal(state)
            journal.record_thread(job.job_id, token, "example-alias-thread", self.fixture.root)
            journal.record_turn(job.job_id, token, "example-alias-turn")
            state.heartbeat_provider_job(
                job.job_id,
                token,
                lease_seconds=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
            client = Client(StoredTurnOutcome("active"))
            self.assertTrue(
                recover_codex_job(
                    state,
                    config,
                    self.fixture.fixture.registry,
                    worker_id="example-alias-recovery",
                    agent_id=alias,
                    client_factory=lambda: cast(CodexAppServerClient, client),
                )
            )
            self.assertEqual(state.get_provider_job(job.job_id).status, "indeterminate")
            self.assertEqual(
                [call[0] for call in client.calls],
                ["read", "close"],
            )
            observer = TurnObservation(state, config)
            self.assertEqual(
                observer._claim(),
                (job.job_id, "example-alias-thread", "example-alias-turn", self.fixture.root),
            )
            observer.apply_outcome(
                job.job_id,
                "example-alias-thread",
                "example-alias-turn",
                self.fixture.root,
                StoredTurnOutcome("completed", TurnResult("Example alias final", None, None)),
            )
            self.assertEqual(state.get_provider_job(job.job_id).status, "result_ready")
            outbox = state.get_telegram_outbox_for_job(job.job_id)
            assert outbox is not None
            self.assertEqual(outbox.sender_agent_id, alias)
            excerpt = state._connection.execute(
                "SELECT agent_id,provider,response_excerpt FROM external_turn_excerpts WHERE agent_id=?",
                (alias,),
            ).fetchone()
            assert excerpt is not None
            self.assertEqual(tuple(excerpt), (alias, "codex", "Example alias final"))
            cursor = state._connection.execute(
                "SELECT observer_agent_id,last_turn_id FROM visible_context_cursors"
            ).fetchone()
            assert cursor is not None
            self.assertEqual(tuple(cursor), (alias, watermark))


if __name__ == "__main__":
    unittest.main()
