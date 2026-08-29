from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.external_admission import (
    consume_pending_handoff,
    is_active_agent,
    peek_pending_handoff,
    record_external_turn,
)
from hermes_codex_router.state import HubState


class ExternalAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "hub.db"
        state = HubState.open(self.path)
        self.topic = state.observe_topic(
            project_id="pythia",
            chat_id=-1001234567890,
            thread_id=73,
            title="main",
        )
        state.activate_agent(self.topic.topic_id, "hermes", "provider-selected", "high")
        state.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_allows_only_exact_topic_selected_for_agent(self) -> None:
        self.assertTrue(is_active_agent(self.path, -1001234567890, 73, agent_id="hermes"))
        self.assertFalse(is_active_agent(self.path, -1001234567890, 74, agent_id="hermes"))
        self.assertFalse(is_active_agent(self.path, -1001234567890, 73, agent_id="codex"))

    def test_missing_or_invalid_database_fails_closed(self) -> None:
        self.assertFalse(
            is_active_agent(
                Path(self.tempdir.name) / "missing.db",
                -1001234567890,
                73,
                agent_id="hermes",
            )
        )
        broken = Path(self.tempdir.name) / "broken.db"
        broken.write_text("not sqlite", encoding="utf-8")
        self.assertFalse(is_active_agent(broken, -1001234567890, 73, agent_id="hermes"))

    def test_handoff_is_peeked_then_consumed_explicitly(self) -> None:
        state = HubState.open(self.path)
        staged = state.stage_handoff(
            self.topic.topic_id,
            target_agent_id="hermes",
            source_agent_id="codex",
            text="context",
        )
        state.close()
        found = peek_pending_handoff(self.path, -1001234567890, 73, target_agent_id="hermes")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.text, "context")
        self.assertTrue(consume_pending_handoff(self.path, staged.handoff_id))
        self.assertIsNone(
            peek_pending_handoff(self.path, -1001234567890, 73, target_agent_id="hermes")
        )

    def test_records_only_visible_turn_for_current_active_agent(self) -> None:
        self.assertTrue(
            record_external_turn(
                self.path,
                chat_id=-1001234567890,
                thread_id=73,
                agent_id="hermes",
                provider_session_id="hs-1",
                model="glm",
                provider="opencode-go",
                user_excerpt="question",
                response_excerpt="answer",
            )
        )
        self.assertFalse(
            record_external_turn(
                self.path,
                chat_id=-1001234567890,
                thread_id=73,
                agent_id="codex",
                provider_session_id="cs-1",
                model="gpt",
                provider="openai",
                user_excerpt="question",
                response_excerpt="answer",
            )
        )


if __name__ == "__main__":
    unittest.main()
