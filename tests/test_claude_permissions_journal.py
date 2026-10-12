from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import uuid4

from hermes_codex_router.claude_permission_binding import permission_notice_is_current
from hermes_codex_router.claude_permission_host import hosted_claude_launch
from hermes_codex_router.claude_permission_protocol import ProtectedPayload
from hermes_codex_router.claude_permissions_journal import ClaudePermissionJournal
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ProviderUnavailableError
from hermes_codex_router.hub_config import AgentDefinition, HubConfig
from hermes_codex_router.state import HubState, StateError


class ClaudePermissionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "example-project"
        self.root.mkdir()
        self.state = HubState.open(Path(directory.name) / "state.db", codex_permission_profile=None)
        self.addCleanup(self.state.close)
        topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example",
            execution_root=self.root,
        )
        self.session = self.state.activate_agent(topic.topic_id, "claude", "example-model", "high")
        self.job, _ = self.state.enqueue_provider_job(
            idempotency_key="example:1",
            chat_id=topic.chat_id,
            message_id=1,
            topic_id=topic.topic_id,
            agent_id="claude",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model="example-model",
            effort="high",
            payload_text="Example request",
        )
        leased = self.state.lease_provider_job("claude", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.token = leased.lease_token
        self.state.mark_provider_job_executing(self.job.job_id, self.token)
        binding = ExecutionJournal(self.state).prepare_claude_session(
            self.job.job_id, self.token, self.root
        )
        self.journal = ClaudePermissionJournal(self.state)
        self.launch = self.journal.open_launch(
            self.job.job_id, self.token, binding.session_id, self.root
        )

    def request(self):
        return self.journal.prepare(
            self.launch,
            str(uuid4()),
            "a" * 64,
            "Write",
            {"file_path": "example.txt", "content": "Example"},
        )

    def test_decision_consumed_once_and_input_is_not_persisted(self) -> None:
        payload = self.request()
        self.journal.consume(self.launch, payload, "allow")
        with self.assertRaises(StateError):
            self.journal.consume(self.launch, payload, "allow")
        rows = self.state._connection.execute("SELECT * FROM claude_permission_requests").fetchall()
        self.assertNotIn("example.txt", str([tuple(row) for row in rows]))

    def test_changed_payload_cannot_consume(self) -> None:
        payload = self.request()
        with self.assertRaises(StateError):
            self.journal.consume(self.launch, payload.replace("Example", "Changed"), "allow")

    def test_close_revokes_pending_and_launch_cannot_reopen(self) -> None:
        payload = self.request()
        self.journal.close_launch(self.launch)
        with self.assertRaises(StateError):
            self.journal.consume(self.launch, payload, "allow")
        with self.assertRaises(StateError):
            self.journal.open_launch(self.job.job_id, self.token, self.launch.session_id, self.root)

    def test_session_generation_writer_root_and_lease_are_rechecked(self) -> None:
        for statement, restore, values in (
            (
                "UPDATE agent_sessions SET generation=generation+1",
                "UPDATE agent_sessions SET generation=generation-1",
                (),
            ),
            (
                "UPDATE agent_sessions SET writer_mode='local'",
                "UPDATE agent_sessions SET writer_mode='telegram'",
                (),
            ),
            (
                "UPDATE topics SET execution_scope='project:other-example'",
                "UPDATE topics SET execution_scope=?",
                (f"root:{self.root}",),
            ),
            (
                "UPDATE provider_jobs SET lease_token='different'",
                "UPDATE provider_jobs SET lease_token=?",
                (self.token,),
            ),
            (
                "UPDATE provider_execution_checkpoints SET completed_text='Done'",
                "UPDATE provider_execution_checkpoints SET completed_text=NULL",
                (),
            ),
            ("UPDATE topics SET thread_id=8", "UPDATE topics SET thread_id=7", ()),
            (
                "UPDATE topics SET project_id='other-example'",
                "UPDATE topics SET project_id='example-project'",
                (),
            ),
        ):
            with self.subTest(statement=statement):
                payload = self.request()
                with self.state._connection:
                    self.state._connection.execute(statement)
                with self.assertRaises(StateError):
                    self.journal.consume(self.launch, payload, "allow")
                with self.state._connection:
                    self.state._connection.execute(restore, values)

    def test_timeout_denies_and_stop_cannot_be_approved(self) -> None:
        payload = self.request()
        with self.state._connection:
            self.state._connection.execute(
                "INSERT INTO provider_stop_requests VALUES (?,?,?,?,?,'pending',0,?,NULL)",
                ("example-stop", self.job.topic_id, self.job.chat_id, 2, "claude", "9999"),
            )
        with self.assertRaises(StateError):
            self.journal.consume(self.launch, payload, "allow")

    def test_failed_wait_revokes_only_pending_request_and_suppresses_notice(self) -> None:
        payload = self.request()
        nonce = ProtectedPayload.parse(payload).request_nonce

        def notice_is_current() -> bool:
            return permission_notice_is_current(
                self.state._connection,
                job_id=self.job.job_id,
                event_key=f"claude-permission:{nonce}",
                chat_id=self.job.chat_id,
                thread_id=7,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        self.assertTrue(notice_is_current())
        self.journal.revoke_request(self.launch, nonce)
        self.assertFalse(notice_is_current())
        with self.assertRaises(StateError):
            self.journal.consume(self.launch, payload, "allow")
        next_payload = self.request()
        self.journal.consume(self.launch, next_payload, "allow")
        self.journal.revoke_request(self.launch, ProtectedPayload.parse(next_payload).request_nonce)
        statuses = self.state._connection.execute(
            "SELECT status FROM claude_permission_requests ORDER BY rowid"
        ).fetchall()
        self.assertEqual([row[0] for row in statuses], ["revoked", "allow"])

    def test_wait_notice_cannot_follow_replaced_generation(self) -> None:
        nonce = ProtectedPayload.parse(self.request()).request_nonce
        with self.state._connection:
            self.state._connection.execute("UPDATE agent_sessions SET generation=generation+1")
        self.assertFalse(
            permission_notice_is_current(
                self.state._connection,
                job_id=self.job.job_id,
                event_key=f"claude-permission:{nonce}",
                chat_id=self.job.chat_id,
                thread_id=7,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

    def test_session_mode_and_home_cannot_change_on_resume(self) -> None:
        def bind(mode: str, home: Path, is_new: bool = True) -> None:
            self.journal.bind_session_mode(
                self.job.job_id,
                self.token,
                self.launch.session_id,
                self.root,
                mode=mode,
                home=home,
                is_new=is_new,
            )

        with self.assertRaises(StateError):
            bind("file_tools", self.root, is_new=False)
        bind("file_tools", self.root)
        bind("file_tools", self.root, is_new=False)
        with self.assertRaises(StateError):
            bind("text_only", self.root)
        with self.assertRaises(StateError):
            bind("file_tools", self.root / "different")
        row = self.state._connection.execute(
            "SELECT * FROM claude_permission_session_modes"
        ).fetchone()
        self.assertNotIn(str(self.root), str(tuple(row)))

    def test_effective_text_store_is_pinned_and_lease_failure_does_not_suggest_reset(self) -> None:
        config = cast(HubConfig, SimpleNamespace(claude_file_permissions=None))
        agent = cast(AgentDefinition, SimpleNamespace(runtime="claude"))

        def launch():
            return hosted_claude_launch(
                config,
                self.state,
                agent,
                self.job,
                self.token,
                self.launch.session_id,
                self.root,
                is_new=True,
            )

        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "example-config"}):
            with launch() as hosted:
                self.assertIsNone(hosted)
            row = self.state._connection.execute(
                "SELECT home_digest FROM claude_permission_session_modes"
            ).fetchone()
            self.assertEqual(
                row[0], hashlib.sha256(str(self.root / "example-config").encode()).hexdigest()
            )
            with self.state._connection:
                self.state._connection.execute("UPDATE agent_sessions SET writer_mode='local'")
            with self.assertRaises(ProviderUnavailableError) as failure:
                with launch():
                    self.fail("changed binding started productive execution")
            self.assertEqual(failure.exception.code, "claude_permission_host_unverified")
            self.assertNotIn("/new", str(failure.exception))
