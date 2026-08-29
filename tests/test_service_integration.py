from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.codex_appserver import CodexThread, RateLimits, TurnResult
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str]] = []

    def send_html(self, chat_id: int, thread_id: int, text: str, **_: object) -> None:
        self.sent.append((chat_id, thread_id, text))


class FakeClient:
    def __init__(self) -> None:
        self.started = 0
        self.resumed = 0

    def start_thread(self, **_: object) -> CodexThread:
        self.started += 1
        return CodexThread("thread-1", Path.cwd(), "gpt-5.6-sol", "openai")

    def resume_thread(self, **_: object) -> CodexThread:
        self.resumed += 1
        return CodexThread("thread-1", Path.cwd(), "gpt-5.6-sol", "openai")

    def start_turn(self, **_: object) -> str:
        return "turn-1"

    def wait_for_turn(self, _: str) -> TurnResult:
        return TurnResult("Visible answer", 1000, 100)

    def read_rate_limits(self) -> RateLimits:
        return RateLimits(None, None)

    def close(self) -> None:
        pass


class FakeSupervisor:
    def __init__(self, client: FakeClient) -> None:
        self.value = client

    def client(self) -> FakeClient:
        return self.value


def update(message_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "message_thread_id": 77,
            "is_topic_message": True,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Private"},
            "text": text,
        },
    }


class ServiceIntegrationTests(unittest.TestCase):
    def test_message_reply_dedup_restart_and_resume_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project_root = base / "Project"
            (project_root / ".git").mkdir(parents=True)
            state_path = base / "state.db"
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=base / "projects.json",
                state_path=state_path,
                codex_socket_path=base / "codex.sock",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(ProjectBinding("project", -1001234567890),),
                agents=(
                    AgentDefinition(
                        "codex",
                        "Codex",
                        "project_codex_bot",
                        "codex",
                        None,
                        True,
                        False,
                        "gpt-5.6-sol",
                        "high",
                    ),
                ),
            )
            registry = ProjectRegistry(
                1,
                (base,),
                (Project("project", "Project", "Project", project_root),),
            )
            client = FakeClient()
            telegram = FakeTelegram()

            def service() -> ProjectHubService:
                value = ProjectHubService.__new__(ProjectHubService)
                value.config = config
                value.registry = registry
                value.state = HubState.open(state_path)
                value.agent = config.agents[0]
                value.telegram = cast(Any, telegram)
                value.supervisor = cast(Any, FakeSupervisor(client))
                value.usernames = {"codex": "project_codex_bot"}
                return value

            first = service()
            self.assertTrue(first.handle_update(update(1, "Run the safe task")))
            self.assertFalse(first.handle_update(update(1, "Run the safe task")))
            first.state.close()

            resumed = service()
            self.assertTrue(resumed.handle_update(update(2, "Continue")))
            self.assertTrue(resumed.handle_update(update(3, "/status")))
            resumed.state.close()

        self.assertEqual(client.started, 1)
        self.assertEqual(client.resumed, 1)
        self.assertEqual(len(telegram.sent), 3)
        self.assertIn("Visible answer", telegram.sent[0][2])
        self.assertIn("Writer: telegram", telegram.sent[-1][2])


if __name__ == "__main__":
    unittest.main()
