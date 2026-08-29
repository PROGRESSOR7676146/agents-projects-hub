from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.state import HubState, StateError


class MultiProjectIsolationTests(unittest.TestCase):
    def test_two_projects_never_share_topic_or_provider_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            try:
                first_topic = state.observe_topic(
                    project_id="alpha",
                    chat_id=-1001111111111,
                    thread_id=7,
                    title="Backend",
                )
                second_topic = state.observe_topic(
                    project_id="beta",
                    chat_id=-1002222222222,
                    thread_id=7,
                    title="Backend",
                )
                first = state.activate_agent(first_topic.topic_id, "codex", "model", "high")
                second = state.activate_agent(second_topic.topic_id, "codex", "model", "high")
                first = state.bind_provider_session(first.session_id, "thread-alpha", None)
                second = state.bind_provider_session(second.session_id, "thread-beta", None)

                self.assertNotEqual(first.topic_id, second.topic_id)
                self.assertNotEqual(first.provider_session_id, second.provider_session_id)
                self.assertEqual(
                    state.find_topic(-1001111111111, 7).project_id,  # type: ignore[union-attr]
                    "alpha",
                )
                with self.assertRaisesRegex(StateError, "another project"):
                    state.observe_topic(
                        project_id="beta",
                        chat_id=-1001111111111,
                        thread_id=7,
                        title="Spoofed title",
                    )
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
