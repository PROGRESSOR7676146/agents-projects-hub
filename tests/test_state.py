from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_codex_router.state import HubState


class HubStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "hub.db"
        self.state = HubState.open(self.path)
        self.topic = self.state.observe_topic(
            project_id="alpha",
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
            project_id="alpha",
            chat_id=-1001234567890,
            thread_id=77,
            title="API renamed",
        )
        self.assertEqual(renamed.topic_id, self.topic.topic_id)
        self.assertEqual(renamed.title, "API renamed")

    def test_private_chat_topic_uses_positive_owner_identity(self) -> None:
        direct = self.state.observe_topic(
            project_id="alpha",
            chat_id=123456789,
            thread_id=1,
            title="Direct",
        )
        self.assertEqual((direct.chat_id, direct.thread_id), (123456789, 1))

    def test_switching_agents_preserves_and_resumes_each_provider_session(self) -> None:
        codex = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "gemini", "gemini-3-pro", "high"
        )
        promoted = self.state.activate_agent(self.topic.topic_id, "gemini", "gemini-3-pro", "high")

        self.assertEqual(promoted.session_id, satellite.session_id)
        self.assertEqual(self.state.get_session(codex.session_id).status, "satellite")
        self.assertEqual(promoted.status, "active")

        resumed = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        self.assertEqual(resumed.session_id, codex.session_id)
        self.assertEqual(self.state.get_session(promoted.session_id).status, "satellite")

    def test_new_resets_only_active_and_preserves_satellite(self) -> None:
        first = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "gemini", "gemini-3-pro", "high"
        )
        replacement = self.state.new_active_session(self.topic.topic_id)

        self.assertNotEqual(first.session_id, replacement.session_id)
        self.assertEqual(self.state.get_session(first.session_id).status, "archived")
        self.assertEqual(self.state.get_session(satellite.session_id).status, "satellite")

    def test_replace_active_session_uses_selected_model_and_effort(self) -> None:
        previous = self.state.activate_agent(
            self.topic.topic_id, "opencode", "provider-selected", "high"
        )
        replacement = self.state.replace_active_session(
            self.topic.topic_id, model="opencode-go/glm-5.3", effort="max"
        )
        self.assertEqual(replacement.agent_id, "opencode")
        self.assertEqual(replacement.model, "opencode-go/glm-5.3")
        self.assertEqual(replacement.effort, "max")
        self.assertEqual(replacement.generation, previous.generation + 1)

    def test_duplicate_telegram_message_is_claimed_once_across_bot_tokens(self) -> None:
        self.assertTrue(self.state.claim_message(-1001234567890, 501, observer_agent_id="codex"))
        self.assertFalse(self.state.claim_message(-1001234567890, 501, observer_agent_id="gemini"))

    def test_provider_session_binding_is_persisted(self) -> None:
        session = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        bound = self.state.bind_provider_session(session.session_id, "thread-123", "tab-name")
        self.assertEqual(bound.provider_session_id, "thread-123")
        self.assertEqual(bound.terminal_name, "tab-name")
        found = self.state.find_topic(self.topic.chat_id, self.topic.thread_id)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.topic_id, self.topic.topic_id)
        self.assertEqual(found.active_agent_id, "codex")

    def test_session_context_remaining_is_persisted(self) -> None:
        session = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        updated = self.state.set_context_remaining(session.session_id, 73.25)
        self.assertEqual(updated.context_remaining_percent, 73.25)

    def test_bot_update_offset_is_persisted_monotonically_by_caller(self) -> None:
        self.assertIsNone(self.state.get_bot_offset("codex"))
        self.state.set_bot_offset("codex", 514951014)
        self.assertEqual(self.state.get_bot_offset("codex"), 514951014)

    def test_writer_mode_persists_terminal_and_local_takeover(self) -> None:
        session = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        terminal = self.state.set_writer_mode(session.session_id, "terminal")
        self.assertEqual(terminal.writer_mode, "terminal")
        local = self.state.set_writer_mode(session.session_id, "local")
        self.assertEqual(local.writer_mode, "local")
        telegram = self.state.set_writer_mode(session.session_id, "telegram")
        self.assertEqual(telegram.writer_mode, "telegram")

    def test_topic_running_dispatch_is_detected(self) -> None:
        session = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        dispatch_id = self.state.start_dispatch(
            chat_id=self.topic.chat_id,
            message_id=701,
            topic_id=self.topic.topic_id,
            agent_id=session.agent_id,
        )
        self.assertTrue(self.state.topic_has_running_dispatch(self.topic.topic_id))
        self.state.finish_dispatch(dispatch_id, success=True)
        self.assertFalse(self.state.topic_has_running_dispatch(self.topic.topic_id))

    def test_active_agent_lookup_uses_numeric_topic_identity(self) -> None:
        self.assertIsNone(
            self.state.active_agent_for_route(self.topic.chat_id, self.topic.thread_id)
        )
        self.state.activate_agent(self.topic.topic_id, "hermes", "provider-selected", "high")
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
        context = self.state.recent_external_context(self.topic.topic_id, "hermes", limit=3)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertNotIn("u6", context)
        self.assertLess(context.index("u7"), context.index("u9"))

    def test_unseen_topic_context_excludes_observer_and_advances_only_when_acknowledged(
        self,
    ) -> None:
        first = self.state.record_visible_turn(
            self.topic.topic_id,
            agent_id="antigravity",
            provider="antigravity",
            model="provider-selected",
            user_excerpt="question for the satellite",
            response_excerpt="satellite answer",
        )
        self.state.record_visible_turn(
            self.topic.topic_id,
            agent_id="codex",
            provider="openai",
            model="gpt-5.6-sol",
            user_excerpt="question for main",
            response_excerpt="main answer",
        )

        context, watermark = self.state.unseen_visible_context(self.topic.topic_id, "codex")
        self.assertIsNotNone(context)
        assert context is not None
        self.assertIn("question for the satellite", context)
        self.assertIn("satellite answer", context)
        self.assertNotIn("question for main", context)
        self.assertEqual(watermark, first)

        repeated, repeated_watermark = self.state.unseen_visible_context(
            self.topic.topic_id, "codex"
        )
        self.assertEqual((repeated, repeated_watermark), (context, watermark))
        assert watermark is not None
        self.state.acknowledge_visible_context(self.topic.topic_id, "codex", watermark)
        self.assertEqual(
            self.state.unseen_visible_context(self.topic.topic_id, "codex"),
            (None, None),
        )

    def test_dispatch_health_tracks_running_and_completed_turns(self) -> None:
        dispatch_id = self.state.start_dispatch(
            chat_id=self.topic.chat_id,
            message_id=700,
            topic_id=self.topic.topic_id,
            agent_id="codex",
        )
        running = self.state.status_snapshot()
        self.assertEqual(running["dispatch_counts"], {"running": 1})
        self.state.finish_dispatch(dispatch_id, success=True)
        completed = self.state.status_snapshot()
        self.assertEqual(completed["dispatch_counts"], {"completed": 1})
        self.assertEqual(completed["pending_dispatches"], [])

    def test_lane_binding_requires_same_project_and_is_unique_per_topic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lane"
            path.mkdir()
            self.state.register_lane(
                lane_id="backend",
                project_id="alpha",
                worktree_path=path,
                branch_name="lane/backend",
            )
            bound = self.state.bind_lane("backend", self.topic.topic_id)
            self.assertEqual(bound["topic_id"], self.topic.topic_id)

            other = Path(directory) / "other"
            other.mkdir()
            self.state.register_lane(
                lane_id="frontend",
                project_id="alpha",
                worktree_path=other,
                branch_name="lane/frontend",
            )
            with self.assertRaisesRegex(RuntimeError, "already bound"):
                self.state.bind_lane("frontend", self.topic.topic_id)

    def test_alert_delivery_claim_has_cooldown(self) -> None:
        self.assertTrue(self.state.claim_alert_delivery("codex:quota", cooldown_seconds=3600))
        self.assertFalse(self.state.claim_alert_delivery("codex:quota", cooldown_seconds=3600))
        self.state.release_alert_delivery("codex:quota")
        self.assertTrue(self.state.claim_alert_delivery("codex:quota", cooldown_seconds=3600))

    def test_concurrent_alert_delivery_has_exactly_one_winner(self) -> None:
        barrier = threading.Barrier(2)

        def claim() -> bool:
            state = HubState.open(self.path)
            try:
                barrier.wait()
                return state.claim_alert_delivery("worker:offline", cooldown_seconds=3600)
            finally:
                state.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: claim(), range(2)))

        self.assertEqual(sorted(results), [False, True])

    def test_runtime_counter_baselines_and_advances_monotonically(self) -> None:
        self.assertIsNone(self.state.observe_runtime_counter("codex:429", 4))
        self.assertEqual(self.state.observe_runtime_counter("codex:429", 6), 4)
        self.assertEqual(self.state.observe_runtime_counter("codex:429", 5), 6)

    def test_lists_only_topics_where_agent_is_active(self) -> None:
        self.state.activate_agent(self.topic.topic_id, "codex", "gpt-5.6-sol", "high")
        second = self.state.observe_topic(
            project_id="alpha", chat_id=-1001234567890, thread_id=78, title="Other"
        )
        self.state.activate_agent(second.topic_id, "opencode", "provider-selected", "high")
        active = self.state.active_topics_for_agent("codex")
        self.assertEqual(
            [(item.chat_id, item.thread_id) for item in active], [(-1001234567890, 77)]
        )

    def test_cleaned_lane_is_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lane"
            path.mkdir()
            self.state.register_lane(
                lane_id="cleanup",
                project_id="alpha",
                worktree_path=path,
                branch_name="lane/cleanup",
            )
            self.state.archive_lane("cleanup")
            self.state.mark_lane_cleaned("cleanup")
            self.assertIsNotNone(self.state.get_lane("cleanup")["cleaned_at"])
            with self.assertRaisesRegex(RuntimeError, "already cleaned"):
                self.state.mark_lane_cleaned("cleanup")


if __name__ == "__main__":
    unittest.main()
