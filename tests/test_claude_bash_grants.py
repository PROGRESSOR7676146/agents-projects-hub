"""Owner Bash consent survives turns, never a worker or authenticated daemon restart."""

from __future__ import annotations

import dataclasses
import unittest
from unittest.mock import patch
from uuid import uuid4

from hermes_codex_router import claude_permission_host as host
from hermes_codex_router.claude_bash_grants import BashGrantCache
from hermes_codex_router.claude_permission_protocol import event_digest
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import StateError
from hermes_codex_router.tlive_permissions import TlivePermissionError
from tests import test_claude_permission_host_roundtrip as fixtures


class BashGrantTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PermissionHostRoundtripTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.server.launch = dataclasses.replace(self.fixture.launch, profile="developer")
        self.fixture.peer.release.set()
        self.cache_patch = patch.object(host, "BASH_GRANTS", BashGrantCache())
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def command(self, tool="Bash", tool_input=None, **changes):
        event = self.fixture._event(
            tool_name=tool,
            tool_input=tool_input or {"command": "printf example"},
            **changes,
        )
        wire = {
            "kind": "claude.permission.request",
            "version": 1,
            "nonce": str(uuid4()),
            "event": event,
            "eventDigest": event_digest(event),
        }
        return self.fixture.server._decision(self.fixture.state, wire)

    def grant(self):
        self.fixture.peer.decision = "allow_bash_until_restart"
        self.assertEqual(self.command(), "allow")

    def test_explicit_grant_reuses_consent_without_wait_notices(self):
        self.grant()
        self.assertEqual(self.command(tool_input={"command": "git status --short"}), "allow")
        self.assertEqual(len(self.fixture.peer.requests), 1)
        self.assertEqual(self.fixture._statuses(), ["allow", "allow"])
        count = self.fixture.state._connection.execute(
            "SELECT count(*) FROM task_lifecycle_notices WHERE kind='claude_permission_wait'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_allow_once_and_deny_never_create_grant(self):
        for decision in ("allow", "deny", "allow"):
            self.fixture.peer.decision = decision
            self.assertEqual(self.command(), decision)
        self.assertEqual(len(self.fixture.peer.requests), 3)

    def test_tlive_restart_requires_new_human_card(self):
        self.grant()
        self.fixture.peer.epoch = str(uuid4())
        self.fixture.peer.decision = "deny"
        self.assertEqual(self.command(), "deny")
        self.assertEqual(len(self.fixture.peer.requests), 2)

    def test_worker_restart_starts_with_empty_cache(self):
        self.grant()
        with patch.object(host, "BASH_GRANTS", BashGrantCache()):
            self.fixture.peer.decision = "deny"
            self.assertEqual(self.command(), "deny")
        self.assertEqual(len(self.fixture.peer.requests), 2)

    def next_launch(self, worker_id="example-worker"):
        fixture = self.fixture
        fixture.journal.close_launch(fixture.server.launch)
        fixture.state.commit_provider_result(
            fixture.job.job_id,
            fixture.token,
            visible_response="Example result",
            sender_agent_id="claude",
            telegram_html="Example result",
            provider_session_id=fixture.native_id,
        )
        outbox = fixture.state.lease_telegram_outbox("claude", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        part = fixture.state.next_telegram_outbox_part(outbox.outbox_id, outbox.lease_token)
        fixture.state.delivery.begin_outbox_send(
            outbox.outbox_id, outbox.lease_token, part.part_index
        )
        fixture.state.mark_telegram_outbox_delivered(
            outbox.outbox_id,
            outbox.lease_token,
            telegram_message_id=124,
        )
        job, _ = fixture.state.enqueue_provider_job(
            idempotency_key="example:2",
            chat_id=fixture.job.chat_id,
            message_id=2,
            topic_id=fixture.job.topic_id,
            agent_id="claude",
            session_id=fixture.job.session_id,
            session_generation=fixture.launch.generation,
            model="example-model",
            effort="high",
            payload_text="Another example task",
        )
        leased = fixture.state.lease_provider_job("claude", worker_id)
        assert leased is not None and leased.lease_token is not None
        fixture.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        native = ExecutionJournal(fixture.state).prepare_claude_session(
            job.job_id,
            leased.lease_token,
            fixture.root,
        )
        self.assertEqual(native.session_id, fixture.native_id)
        fixture.server.launch = fixture.journal.open_launch(
            job.job_id, leased.lease_token, fixture.native_id, fixture.root, profile="developer"
        )

    def test_next_launch_same_session_retains_grant(self):
        self.grant()
        self.next_launch()
        self.assertEqual(self.command(), "allow")
        self.assertEqual(len(self.fixture.peer.requests), 1)

    def test_next_launch_other_worker_requires_new_human_card(self):
        self.grant()
        self.next_launch("example-other-worker")
        self.fixture.peer.decision = "deny"
        self.assertEqual(self.command(), "deny")
        self.assertEqual(len(self.fixture.peer.requests), 2)

    def test_missing_tlive_denies_even_with_grant(self):
        self.grant()
        with patch.object(self.fixture.server.client, "hello", side_effect=TlivePermissionError()):
            with self.assertRaises(TlivePermissionError):
                self.command()

    def test_stop_and_writer_checks_still_apply_with_grant(self):
        self.grant()
        self.fixture.state._connection.execute(
            "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
            (self.fixture.job.session_id,),
        )
        self.fixture.state._connection.commit()
        with self.assertRaises(StateError):
            self.command()
        self.assertEqual(self.fixture._statuses(), ["allow"])

    def test_grant_cannot_cover_write_or_native_suggestions(self):
        self.grant()
        self.fixture.peer.decision = "deny"
        self.assertEqual(
            self.command("Write", {"file_path": "example.txt", "content": "x"}), "deny"
        )
        with self.assertRaises(ValueError):
            self.command(permission_updates=[{"type": "allow"}])
        self.assertEqual(len(self.fixture.peer.requests), 2)

    def test_bash_grant_result_on_other_tool_is_rejected(self):
        self.fixture.peer.decision = "allow_bash_until_restart"
        with self.assertRaises(TlivePermissionError):
            self.command("Write", {"file_path": "example.txt", "content": "x"})

    def test_forged_actor_cannot_create_grant(self):
        self.fixture.peer.decision = "allow_bash_until_restart"
        actor_changes: tuple[dict[str, object], ...] = (
            {"isBot": True},
            {"userId": "99"},
            {"data": "hp:wrong:b"},
        )
        for change in actor_changes:
            self.fixture.peer.actor_change = change
            with self.assertRaises(TlivePermissionError):
                self.command()
        self.fixture.peer.actor_change = None
        self.fixture.peer.decision = "deny"
        self.assertEqual(self.command(), "deny")
        self.assertEqual(len(self.fixture.peer.requests), 4)

    def test_reused_grant_still_rejects_unsupported_command_options(self):
        self.grant()
        with self.assertRaises(ValueError):
            self.command(tool_input={"command": "pwd", "dangerouslyDisableSandbox": True})

    def test_failed_consumption_does_not_publish_grant(self):
        self.fixture.peer.decision = "allow_bash_until_restart"
        with patch.object(host.ClaudePermissionJournal, "consume", side_effect=StateError()):
            with self.assertRaises(StateError):
                self.command()
        self.fixture.peer.decision = "deny"
        self.assertEqual(self.command(), "deny")
        self.assertEqual(len(self.fixture.peer.requests), 2)

    def test_pending_stop_rejects_cached_consent(self):
        self.grant()
        self.fixture.state._connection.execute(
            "INSERT INTO provider_stop_requests VALUES (?,?,?,?,?,'pending',0,?,NULL)",
            (
                "example-stop",
                self.fixture.job.topic_id,
                self.fixture.job.chat_id,
                2,
                "claude",
                "9999",
            ),
        )
        self.fixture.state._connection.commit()
        with self.assertRaises(StateError):
            self.command()
        self.assertEqual(self.fixture._statuses(), ["allow"])


class GrantCacheTests(unittest.TestCase):
    def test_scope_and_epoch_are_exact_and_cache_is_bounded(self):
        cache = BashGrantCache()
        cache.remember(("example-root", "session-one", 1), "epoch-one")
        for scope in (
            ("example-other", "session-one", 1),
            ("example-root", "session-two", 1),
            ("example-root", "session-one", 2),
        ):
            self.assertFalse(cache.allows(scope, "epoch-one"))
        self.assertFalse(cache.allows(("example-root", "session-one", 1), "epoch-two"))
        self.assertFalse(cache.allows(("example-root", "session-one", 1), "epoch-one"))
        for i in range(257):
            cache.remember(i, "epoch-one")
        self.assertFalse(cache.allows(0, "epoch-one"))
        self.assertTrue(cache.allows(256, "epoch-one"))


if __name__ == "__main__":
    unittest.main()
