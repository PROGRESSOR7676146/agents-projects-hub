from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState, ProviderJobRecord
from hermes_codex_router.topic_panel_snapshot import read_panel_snapshot
from hermes_codex_router.topic_panel_view import render_panel_body

NAMES = {"claude": "Claude", "codex": "Codex"}


class TopicPanelSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / "example-project"
        self.root.mkdir()
        self.state = HubState.open(base / "state.db", codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.db = self.state._connection
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Fictional topic",
            execution_root=self.root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "claude", "example-model", "high"
        )
        self.now = datetime.now(timezone.utc)
        self.message_id = 0

    def enqueue(self) -> ProviderJobRecord:
        self.message_id += 1
        job, created = self.state.enqueue_provider_job(
            idempotency_key=f"example:{self.message_id}",
            chat_id=self.topic.chat_id,
            message_id=self.message_id,
            topic_id=self.topic.topic_id,
            agent_id=self.session.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fictional request",
        )
        self.assertTrue(created)
        return job

    def lease(self) -> str:
        leased = self.state.lease_provider_job("claude", "example-worker")
        assert leased is not None and leased.lease_token is not None
        return leased.lease_token

    def execute(self) -> tuple[str, str]:
        job = self.enqueue()
        token = self.lease()
        self.state.mark_provider_job_executing(job.job_id, token)
        return job.job_id, token

    def read(self):
        return read_panel_snapshot(
            self.db, topic_id=self.topic.topic_id, now=self.now, agent_names=NAMES
        )

    def test_idle_queued_and_preparing_headlines(self) -> None:
        self.assertEqual(self.read().headline, "idle")
        self.enqueue()
        snapshot = self.read()
        self.assertEqual((snapshot.headline, snapshot.queued), ("queued", 1))
        self.lease()
        snapshot = self.read()
        self.assertEqual((snapshot.headline, snapshot.agent), ("preparing", "Claude"))
        self.assertIsNone(snapshot.started_at)

    def test_executing_claude_reports_only_visible_agent_messages(self) -> None:
        job_id, token = self.execute()
        ExecutionJournal(self.state).prepare_claude_session(job_id, token, self.root)
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "executing")
        self.assertEqual(snapshot.model, "example-model")
        self.assertIsNotNone(snapshot.started_at)
        self.assertIsNone(snapshot.activity_at)
        seen = (self.now - timedelta(minutes=3)).isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO provider_visible_items(job_id,item_id,phase,visible_text,created_at) "
                "VALUES(?,?,?,?,?)",
                (job_id, "example-item", "commentary", "Example progress", seen),
            )
        snapshot = self.read()
        self.assertEqual(snapshot.activity_kind, "message")
        self.assertEqual(snapshot.activity_at, datetime.fromisoformat(seen))
        self.assertIn("Последнее событие: сообщение агента", render_panel_body(snapshot))

    def test_queued_and_stopping_states_keep_job_identity(self) -> None:
        self.enqueue()
        snapshot = self.read()
        self.assertEqual((snapshot.agent, snapshot.model), ("Claude", "example-model"))
        self.assertIn("⏳ В очереди · Claude", render_panel_body(snapshot))
        self.lease()
        with self.db:
            self.db.execute(
                "INSERT INTO provider_stop_requests(request_id,topic_id,chat_id,message_id,"
                "target_agent_id,status,cancelled_queued_count,created_at) "
                "VALUES(?,?,?,?,?,'pending',0,?)",
                (
                    str(uuid.uuid4()),
                    self.topic.topic_id,
                    self.topic.chat_id,
                    98,
                    "claude",
                    self.now.isoformat(),
                ),
            )
        body = render_panel_body(self.read())
        self.assertIn("🟠 Остановка запрошена · Claude", body)
        self.assertIn("Модель: example-model", body)

    def test_pending_hold_counts_separately_from_queue(self) -> None:
        cause_id, _ = self.execute()
        held = self.enqueue()
        self.enqueue()
        with self.db:
            self.db.execute(
                "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at,decision) "
                "VALUES(?,?,?,'pending')",
                (held.job_id, cause_id, self.now.isoformat()),
            )
        snapshot = self.read()
        self.assertEqual((snapshot.queued, snapshot.held), (1, 1))

    def test_unresolved_uncertainty_needs_the_owner(self) -> None:
        job_id, _ = self.execute()
        with self.db:
            self.db.execute(
                "UPDATE provider_jobs SET status='indeterminate',lease_token=NULL,"
                "lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?",
                (job_id,),
            )
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "needs_owner")
        self.assertEqual(snapshot.owner_reasons, ("uncertain",))

    def test_pending_stop_and_local_writer_headlines(self) -> None:
        self.execute()
        with self.db:
            self.db.execute(
                "INSERT INTO provider_stop_requests(request_id,topic_id,chat_id,message_id,"
                "target_agent_id,status,cancelled_queued_count,created_at) "
                "VALUES(?,?,?,?,?,'pending',0,?)",
                (
                    str(uuid.uuid4()),
                    self.topic.topic_id,
                    self.topic.chat_id,
                    99,
                    "claude",
                    self.now.isoformat(),
                ),
            )
        self.assertEqual(self.read().headline, "stop_requested")
        with self.db:
            self.db.execute("UPDATE provider_stop_requests SET status='completed'")
            self.db.execute(
                "UPDATE provider_jobs SET status='cancelled',lease_token=NULL,lease_owner=NULL,"
                "lease_expires_at=NULL"
            )
            self.db.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (self.session.session_id,),
            )
        self.assertEqual(self.read().headline, "local_writer")

    def test_reader_never_writes(self) -> None:
        self.execute()
        before = self.db.total_changes
        self.read()
        self.assertEqual(self.db.total_changes, before)

    def test_naive_time_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            read_panel_snapshot(
                self.db,
                topic_id=self.topic.topic_id,
                now=datetime(2026, 11, 20, 12, 0),
                agent_names=NAMES,
            )

    def insert_outbox(self, job_id: str, status: str, delivered_at: str | None = None) -> None:
        now = self.now.isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO telegram_outbox("
                "outbox_id,job_id,sender_agent_id,chat_id,thread_id,telegram_html,status,"
                "available_at,created_at,updated_at,delivered_at,purpose) "
                "SELECT ?,j.job_id,j.agent_id,j.chat_id,t.thread_id,?,?,"
                "?,?,?,?,'provider_result' "
                "FROM provider_jobs j JOIN topics t ON t.topic_id=j.topic_id WHERE j.job_id=?",
                (
                    "example-outbox",
                    "Fictional result",
                    status,
                    now,
                    now,
                    now,
                    delivered_at,
                    job_id,
                ),
            )

    def finish_job(self, job_id: str, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE provider_jobs SET status=?,lease_token=NULL,lease_owner=NULL,"
                "lease_expires_at=NULL WHERE job_id=?",
                (status, job_id),
            )

    def test_codex_accepted_turn_activity_and_lease_binding(self) -> None:
        self.session = self.state.activate_agent(
            self.topic.topic_id, "codex", "example-model", "high"
        )
        job = self.enqueue()
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None and leased.lease_token is not None
        token = leased.lease_token
        self.state.mark_provider_job_executing(job.job_id, token)
        seen = self.now - timedelta(minutes=2)
        with self.db:
            self.db.execute(
                "INSERT INTO task_activity("
                "job_id,lease_token,topic_id,project_id,execution_scope,chat_id,thread_id,"
                "session_id,session_generation,agent_id,provider_thread_id,provider_turn_id,"
                "project_root,mode,last_meaningful_at) "
                "SELECT j.job_id,j.lease_token,j.topic_id,t.project_id,"
                "COALESCE(t.execution_scope,'project:' || t.project_id),j.chat_id,t.thread_id,"
                "j.session_id,j.session_generation,j.agent_id,?,?,?,?,? "
                "FROM provider_jobs j JOIN topics t ON t.topic_id=j.topic_id WHERE j.job_id=?",
                (
                    "example-thread",
                    "example-turn",
                    str(self.root),
                    "tool",
                    seen.isoformat(),
                    job.job_id,
                ),
            )

        for mode, kind, headline, reasons in (
            ("tool", "tool", "executing", ()),
            ("approval", "approval", "needs_owner", ("approval",)),
            ("ordinary", None, "executing", ()),
        ):
            with self.subTest(mode=mode):
                with self.db:
                    self.db.execute(
                        "UPDATE task_activity SET mode=? WHERE job_id=?", (mode, job.job_id)
                    )
                snapshot = self.read()
                self.assertEqual(snapshot.headline, headline)
                self.assertEqual(snapshot.owner_reasons, reasons)
                self.assertEqual(snapshot.activity_kind, kind)
                self.assertEqual(snapshot.activity_at, seen)

        with self.db:
            self.db.execute(
                "UPDATE task_activity SET lease_token=?,mode='approval' WHERE job_id=?",
                (f"{token}-different", job.job_id),
            )
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "executing")
        self.assertEqual(snapshot.owner_reasons, ())
        self.assertIsNone(snapshot.activity_kind)
        self.assertIsNone(snapshot.activity_at)

    def test_claude_permission_wait_requires_pending_unexpired_request(self) -> None:
        job_id, token = self.execute()
        # The permission journal stores expiry as integer epoch milliseconds.
        future = int((self.now + timedelta(minutes=5)).timestamp() * 1000)
        expired = int((self.now - timedelta(minutes=5)).timestamp() * 1000)
        with self.db:
            self.db.execute(
                "INSERT INTO claude_permission_launches("
                "launch_epoch,job_id,binding_digest,status,created_at) VALUES(?,?,?,'active',?)",
                ("example-launch", job_id, "a" * 64, self.now.isoformat()),
            )
            self.db.execute(
                "INSERT INTO claude_permission_requests("
                "request_nonce,launch_epoch,payload_digest,event_digest,expires_at,status) "
                "VALUES(?,?,?,?,?,'pending')",
                ("example-nonce", "example-launch", "b" * 64, "c" * 64, future),
            )
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "needs_owner")
        self.assertEqual(snapshot.owner_reasons, ("approval",))
        # No durable request time exists: the wait is a reason, never an event time.
        self.assertIsNone(snapshot.activity_kind)
        self.assertIsNone(snapshot.activity_at)
        body = render_panel_body(snapshot)
        self.assertIn("🔴 Нужно ваше решение · Claude", body)
        self.assertIn("Модель: example-model", body)

        # An earlier visible message keeps its own kind and time under the wait.
        message_at = self.now - timedelta(minutes=7)
        ExecutionJournal(self.state).prepare_claude_session(job_id, token, self.root)
        with self.db:
            self.db.execute(
                "INSERT INTO provider_visible_items(job_id,item_id,phase,visible_text,created_at) "
                "VALUES(?,?,?,?,?)",
                (job_id, "example-earlier", "commentary", "Example", message_at.isoformat()),
            )
        snapshot = self.read()
        self.assertEqual((snapshot.activity_kind, snapshot.activity_at), ("message", message_at))
        self.assertNotIn("ожидание разрешения", render_panel_body(snapshot))
        with self.db:
            self.db.execute("DELETE FROM provider_visible_items WHERE job_id=?", (job_id,))

        for status, expires_at in (("pending", expired), ("allow", future)):
            with self.subTest(status=status, expires_at=expires_at):
                with self.db:
                    self.db.execute(
                        "UPDATE claude_permission_requests SET status=?,expires_at=? "
                        "WHERE request_nonce=?",
                        (status, expires_at, "example-nonce"),
                    )
                snapshot = self.read()
                self.assertEqual(snapshot.headline, "executing")
                self.assertEqual(snapshot.owner_reasons, ())
                self.assertIsNone(snapshot.activity_kind)
                self.assertIsNone(snapshot.activity_at)

    def test_unknown_final_delivery_holds_topic_until_delivered(self) -> None:
        job_id, _ = self.execute()
        self.insert_outbox(job_id, "unknown")
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "needs_owner")
        self.assertIn("delivery_unknown", snapshot.owner_reasons)

        with self.db:
            self.db.execute(
                "UPDATE telegram_outbox SET status='delivered',delivered_at=? WHERE job_id=?",
                (self.now.isoformat(), job_id),
            )
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "executing")
        self.assertNotIn("delivery_unknown", snapshot.owner_reasons)

    def test_delivered_interim_progress(self) -> None:
        job_id, token = self.execute()
        ExecutionJournal(self.state).prepare_claude_session(job_id, token, self.root)
        delivered = self.now - timedelta(minutes=1)
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO provider_visible_items(job_id,item_id,phase,visible_text,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    job_id,
                    "example-progress-item",
                    "commentary",
                    "Fictional progress",
                    delivered.isoformat(),
                ),
            )
            self.db.execute(
                "INSERT INTO provider_progress_deliveries("
                "progress_id,item_sequence,job_id,sender_agent_id,chat_id,thread_id,"
                "telegram_html,status,available_at,created_at,updated_at,delivered_at) "
                "VALUES(?,?,?,?,?,?,?,'delivered',?,?,?,?)",
                (
                    "example-progress",
                    cursor.lastrowid,
                    job_id,
                    self.session.agent_id,
                    self.topic.chat_id,
                    self.topic.thread_id,
                    "Fictional progress",
                    delivered.isoformat(),
                    delivered.isoformat(),
                    delivered.isoformat(),
                    delivered.isoformat(),
                ),
            )
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "executing")
        self.assertEqual(snapshot.interim_delivered_at, delivered)

    def test_idle_last_result_delivered(self) -> None:
        job_id, _ = self.execute()
        delivered = self.now - timedelta(minutes=1)
        self.finish_job(job_id, "completed")
        self.insert_outbox(job_id, "delivered", delivered.isoformat())
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "idle")
        self.assertEqual(snapshot.last_result, "delivered")
        self.assertEqual(snapshot.last_result_at, delivered)

    def test_idle_last_result_saved(self) -> None:
        job_id, _ = self.execute()
        with self.db:
            self.db.execute(
                "INSERT INTO provider_job_results(result_id,job_id,visible_response,created_at) "
                "VALUES(?,?,?,?)",
                ("example-result", job_id, "Fictional response", self.now.isoformat()),
            )
        self.finish_job(job_id, "result_ready")
        snapshot = self.read()
        self.assertEqual(snapshot.headline, "idle")
        self.assertEqual(snapshot.last_result, "saved")
        self.assertIsNone(snapshot.last_result_at)


if __name__ == "__main__":
    unittest.main()
