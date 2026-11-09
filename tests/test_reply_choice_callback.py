"""A pressed reply choice becomes one ordinary reply; fictional Telegram only."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.reply_choices import session_binding
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import parse_topic_callback
from tests.git_fixtures import init_git_root
from tests.test_service_integration import (
    FakeClient,
    FakeSupervisor,
    FakeTelegram,
    persist_registry,
)

CHAT = -1001234567890


class CallTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        return True


def choice_callback(
    callback_id: str, data: str, *, sender_id: int = 42, binding: str = "0123456789ab"
) -> dict[str, object]:
    return {
        "update_id": 900,
        "callback_query": {
            "id": callback_id,
            "from": {"id": sender_id, "is_bot": False},
            "data": data,
            "message": {
                "message_id": 555,
                "message_thread_id": 77,
                "is_topic_message": True,
                "from": {"id": 9001, "is_bot": True, "username": "example_hub_bot"},
                "chat": {"id": CHAT, "type": "supergroup", "title": "Private"},
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Yes", "callback_data": f"ch:0:{binding}"}],
                        [{"text": "No", "callback_data": f"ch:1:{binding}"}],
                    ]
                },
            },
        },
    }


class ReplyChoiceCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        root = base / "Project"
        init_git_root(root)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("project", CHAT),),
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
        service = ProjectHubService.__new__(ProjectHubService)
        service.config = config
        service.registry = ProjectRegistry(
            1, (base,), (Project("project", "Project", "Project", root),)
        )
        persist_registry(config.registry_path, service.registry)
        service.state = HubState.open(config.state_path, codex_permission_profile=None)
        self.addCleanup(service.state.close)
        service.agent = config.agents[0]
        self.telegram = CallTelegram()
        service.telegram = cast(Any, self.telegram)
        service.usernames = {"codex": "project_codex_bot"}
        self.forwarded: list[dict[str, Any]] = []
        self.service = service
        topic = service.state.observe_topic(
            project_id="project", chat_id=CHAT, thread_id=77, title="Topic 77", execution_root=root
        )
        session = service.state.active_session(topic.topic_id) or service.state.activate_agent(
            topic.topic_id, "codex", "gpt-5.6-sol", "high"
        )
        self.topic_id = topic.topic_id
        self.binding = session_binding(session.session_id)

    def press(self, callback_id: str, data: str, *, sender_id: int = 42) -> bool:
        if data.startswith("ch:") and data != "ch:done":
            data = f"{data}:{self.binding}"
        parsed = parse_topic_callback(
            choice_callback(callback_id, data, sender_id=sender_id, binding=self.binding)
        )
        assert parsed is not None
        service = cast(Any, self.service)
        service.handle_update = lambda update: self.forwarded.append(dict(update)) or True
        return bool(service._handle_callback(parsed))

    def test_first_press_becomes_the_owner_reply_and_marks_the_choice(self) -> None:
        self.assertTrue(self.press("cb-1", "ch:1"))
        self.assertEqual(len(self.forwarded), 1)
        message = self.forwarded[0]["message"]
        self.assertEqual(message["text"], "No")
        self.assertEqual(message["from"], {"id": 42, "is_bot": False})
        self.assertEqual(message["message_id"], 555)
        self.assertEqual(message["message_thread_id"], 77)
        method, params = self.telegram.calls[-1]
        self.assertEqual(method, "editMessageReplyMarkup")
        self.assertEqual(
            json.loads(str(params["reply_markup"])),
            {"inline_keyboard": [[{"text": "✓ No", "callback_data": "ch:done"}]]},
        )
        self.assertEqual(self.telegram.callbacks[-1], ("cb-1", "Отправлено: No"))

    def test_second_press_and_duplicate_delivery_send_nothing(self) -> None:
        self.assertTrue(self.press("cb-1", "ch:0"))
        self.assertTrue(self.press("cb-2", "ch:1"))
        self.assertFalse(self.press("cb-1", "ch:0"))
        self.assertEqual(len(self.forwarded), 1)
        self.assertEqual(self.telegram.callbacks[-2], ("cb-2", "Выбор уже сделан"))

    def test_done_and_out_of_range_buttons_are_refused(self) -> None:
        self.assertTrue(self.press("cb-1", "ch:done"))
        self.assertTrue(self.press("cb-2", "ch:7"))
        self.assertEqual(self.forwarded, [])

    def test_choices_from_an_earlier_session_are_refused(self) -> None:
        self.service.state.new_active_session(self.topic_id)
        self.assertTrue(self.press("cb-1", "ch:0"))
        self.assertEqual(self.forwarded, [])
        self.assertEqual(
            self.telegram.callbacks[-1], ("cb-1", "Эти варианты относятся к прошлой сессии")
        )
        self.assertEqual(self.telegram.calls, [])

    def test_unauthorized_sender_cannot_choose(self) -> None:
        self.assertFalse(self.press("cb-1", "ch:0", sender_id=777))
        self.assertEqual(self.forwarded, [])

    def test_choice_runs_as_an_ordinary_provider_turn(self) -> None:
        client = FakeClient()
        service = cast(Any, self.service)
        service.supervisor = FakeSupervisor(client)
        service._codex_client = None
        self.assertTrue(
            service.handle_update(
                choice_callback("cb-1", f"ch:0:{self.binding}", binding=self.binding)
            )
        )
        self.assertEqual(len(client.prompts), 1)
        self.assertIn("CURRENT USER TURN:\nYes", client.prompts[0])


if __name__ == "__main__":
    unittest.main()
