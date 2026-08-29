from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.state import HubState


class HubStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "hub.db"
        self.state = HubState.open(self.path)
        self.topic = self.state.observe_topic(
            project_id="pythia",
            chat_id=-1001234567890,
            thread_id=77,
            title="Backend",
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def test_database_and_parent_are_private(self) -> None:
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_topic_identity_is_numeric_not_title(self) -> None:
        renamed = self.state.observe_topic(
            project_id="pythia",
            chat_id=-1001234567890,
            thread_id=77,
            title="API renamed",
        )
        self.assertEqual(renamed.topic_id, self.topic.topic_id)
        self.assertEqual(renamed.title, "API renamed")

    def test_promoting_agent_creates_new_active_session_and_keeps_other_satellites(self) -> None:
        codex = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "gemini", "gemini-3-pro", "high"
        )
        promoted = self.state.activate_agent(
            self.topic.topic_id, "gemini", "gemini-3-pro", "high"
        )

        self.assertNotEqual(promoted.session_id, satellite.session_id)
        self.assertEqual(self.state.get_session(codex.session_id).status, "archived")
        self.assertEqual(self.state.get_session(satellite.session_id).status, "archived")
        self.assertEqual(promoted.status, "active")

    def test_new_resets_only_active_and_preserves_satellite(self) -> None:
        first = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "gemini", "gemini-3-pro", "high"
        )
        replacement = self.state.new_active_session(self.topic.topic_id)

        self.assertNotEqual(first.session_id, replacement.session_id)
        self.assertEqual(self.state.get_session(first.session_id).status, "archived")
        self.assertEqual(self.state.get_session(satellite.session_id).status, "satellite")

    def test_new_all_resets_satellites_and_recreates_only_active(self) -> None:
        first = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "gemini", "gemini-3-pro", "high"
        )
        replacement = self.state.new_all_sessions(self.topic.topic_id)

        self.assertEqual(self.state.get_session(first.session_id).status, "archived")
        self.assertEqual(self.state.get_session(satellite.session_id).status, "archived")
        self.assertEqual(replacement.agent_id, "codex")
        self.assertEqual(replacement.status, "active")

    def test_duplicate_telegram_message_is_claimed_once_across_bot_tokens(self) -> None:
        self.assertTrue(
            self.state.claim_message(-1001234567890, 501, observer_agent_id="codex")
        )
        self.assertFalse(
            self.state.claim_message(-1001234567890, 501, observer_agent_id="gemini")
        )

    def test_provider_session_binding_is_persisted(self) -> None:
        session = self.state.activate_agent(
            self.topic.topic_id, "codex", "gpt-5.6-sol", "high"
        )
        bound = self.state.bind_provider_session(session.session_id, "thread-123", "tab-name")
        self.assertEqual(bound.provider_session_id, "thread-123")
        self.assertEqual(bound.terminal_name, "tab-name")
        found = self.state.find_topic(self.topic.chat_id, self.topic.thread_id)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.topic_id, self.topic.topic_id)
        self.assertEqual(found.active_agent_id, "codex")

    def test_bot_update_offset_is_persisted_monotonically_by_caller(self) -> None:
        self.assertIsNone(self.state.get_bot_offset("codex"))
        self.state.set_bot_offset("codex", 514951014)
        self.assertEqual(self.state.get_bot_offset("codex"), 514951014)

    def test_writer_mode_persists_terminal_takeover(self) -> None:
        session = self.state.activate_agent(
            self.topic.topic_id, "codex", "gpt-5.6-sol", "high"
        )
        terminal = self.state.set_writer_mode(session.session_id, "terminal")
        self.assertEqual(terminal.writer_mode, "terminal")
        telegram = self.state.set_writer_mode(session.session_id, "telegram")
        self.assertEqual(telegram.writer_mode, "telegram")

    def test_active_agent_lookup_uses_numeric_topic_identity(self) -> None:
        self.assertIsNone(
            self.state.active_agent_for_route(self.topic.chat_id, self.topic.thread_id)
        )
        self.state.activate_agent(
            self.topic.topic_id, "hermes", "provider-selected", "high"
        )
        self.assertEqual(
            self.state.active_agent_for_route(self.topic.chat_id, self.topic.thread_id),
            "hermes",
        )
        self.assertIsNone(self.state.active_agent_for_route(self.topic.chat_id, 999))

    def test_callback_is_claimed_once(self) -> None:
        self.assertTrue(self.state.claim_callback("cb-1", observer_agent_id="codex"))
        self.assertFalse(self.state.claim_callback("cb-1", observer_agent_id="codex"))

    def test_handoff_is_bounded_and_replaced_per_target(self) -> None:
        first = self.state.stage_handoff(
            self.topic.topic_id,
            target_agent_id="hermes",
            source_agent_id="codex",
            text="first",
        )
        second = self.state.stage_handoff(
            self.topic.topic_id,
            target_agent_id="hermes",
            source_agent_id="codex",
            text="x" * 25000,
        )
        self.assertNotEqual(first.handoff_id, second.handoff_id)
        self.assertEqual(len(second.text), 20000)

    def test_recent_external_context_is_chronological_and_bounded(self) -> None:
        with self.state._connection:
            for index in range(10):
                self.state._connection.execute(
                    """INSERT INTO external_turn_excerpts
                       (topic_id, agent_id, provider_session_id, model, provider,
                        user_excerpt, response_excerpt, created_at)
                       VALUES (?, 'hermes', 'session', 'glm', 'opencode-go', ?, ?, ?)""",
                    (self.topic.topic_id, f"u{index}", f"r{index}", str(index)),
                )
        context = self.state.recent_external_context(
            self.topic.topic_id, "hermes", limit=3
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertNotIn("u6", context)
        self.assertLess(context.index("u7"), context.index("u9"))


if __name__ == "__main__":
    unittest.main()
