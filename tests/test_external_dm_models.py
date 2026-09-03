from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.external_service import ExternalAgentService
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.provider_catalog_cache import CachedProviderModel, CatalogSnapshot
from hermes_codex_router.state import HubState


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str]] = []
        self.markups: list[object] = []
        self.callbacks: list[str] = []

    def send_html(
        self, chat_id: int, thread_id: int, text: str, *, reply_markup: object = None
    ) -> int:
        self.sent.append((chat_id, thread_id, text))
        self.markups.append(reply_markup)
        return len(self.sent)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append(text)


def callbacks(markup: object) -> list[str]:
    if not isinstance(markup, dict):
        return []
    return [
        str(button["callback_data"])
        for row in markup.get("inline_keyboard", [])
        if isinstance(row, list)
        for button in row
        if isinstance(button, dict) and "callback_data" in button
    ]


class ExternalDirectModelTests(unittest.TestCase):
    def test_model_and_effort_are_applied_in_direct_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=base / "projects.json",
                state_path=base / "state.db",
                codex_socket_path=base / "codex.sock",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(ProjectBinding("hub", -1001234567890),),
                agents=(
                    AgentDefinition(
                        "opencode",
                        "OpenCode",
                        "project_opencode_bot",
                        "opencode",
                        None,
                        True,
                        False,
                        "provider-selected",
                        "high",
                    ),
                ),
                direct_message_project_id="hub",
            )
            snapshot = CatalogSnapshot(
                "opencode",
                (
                    CachedProviderModel(
                        "opencode-go/example-model",
                        "Example Model",
                        ("high", "medium"),
                        "abcdef123456",
                    ),
                ),
                datetime.now(timezone.utc),
                "test",
                None,
            )
            service = ExternalAgentService.__new__(ExternalAgentService)
            service.config = config
            service.agent = config.agents[0]
            service.direct_messages_only = True
            service.state_path = base / "opencode-dm.db"
            service.state = HubState.open(service.state_path)
            service.telegram = cast(Any, FakeTelegram())

            message = {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False},
                    "text": "/model",
                },
            }
            with patch.object(ExternalAgentService, "_catalog", return_value=snapshot):
                self.assertTrue(service.handle_update(message))
                choose = callbacks(service.telegram.markups[-1])[0]
                self.assertTrue(
                    service.handle_update(
                        {
                            "update_id": 2,
                            "callback_query": {
                                "id": "choose",
                                "from": {"id": 42},
                                "data": choose,
                                "message": {
                                    "message_id": 2,
                                    "chat": {"id": 42, "type": "private"},
                                },
                            },
                        }
                    )
                )
                use = next(
                    item
                    for item in callbacks(service.telegram.markups[-1])
                    if item.endswith("medium")
                )
                self.assertTrue(
                    service.handle_update(
                        {
                            "update_id": 3,
                            "callback_query": {
                                "id": "use",
                                "from": {"id": 42},
                                "data": use,
                                "message": {
                                    "message_id": 3,
                                    "chat": {"id": 42, "type": "private"},
                                },
                            },
                        }
                    )
                )
            topic = service.state.find_topic(42, 1)
            assert topic is not None
            active = service.state.active_session(topic.topic_id)
            assert active is not None
            self.assertEqual(
                (active.model, active.effort),
                ("opencode-go/example-model", "medium"),
            )
            service.state.close()

    def test_dmrefresh_refreshes_catalog_and_highlights_new_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=base / "projects.json",
                state_path=base / "state.db",
                codex_socket_path=base / "codex.sock",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(ProjectBinding("hub", -1001234567890),),
                agents=(
                    AgentDefinition(
                        "antigravity",
                        "Antigravity",
                        "project_antigravity_bot",
                        "antigravity",
                        None,
                        True,
                        False,
                        "provider-selected",
                        "high",
                    ),
                ),
                direct_message_project_id="hub",
            )
            now = datetime.now(timezone.utc)
            snapshot = CatalogSnapshot(
                "antigravity",
                (
                    CachedProviderModel(
                        "gemini-3.8-flash",
                        "Gemini 3.8 Flash",
                        ("high", "medium"),
                        "abcdef123456",
                        first_seen_at=now,
                    ),
                ),
                now,
                "test",
                None,
            )
            service = ExternalAgentService.__new__(ExternalAgentService)
            service.config = config
            service.agent = config.agents[0]
            service.direct_messages_only = True
            service.state_path = base / "antigravity-dm.db"
            service.state = HubState.open(service.state_path)
            telegram = FakeTelegram()
            service.telegram = cast(Any, telegram)

            with patch.object(ExternalAgentService, "_catalog", return_value=snapshot):
                self.assertTrue(
                    service.handle_update(
                        {
                            "update_id": 1,
                            "callback_query": {
                                "id": "refresh_cb",
                                "from": {"id": 42},
                                "data": "dmrefresh:0",
                                "message": {
                                    "message_id": 10,
                                    "chat": {"id": 42, "type": "private"},
                                },
                            },
                        }
                    )
                )
            markup = telegram.markups[-1]
            assert isinstance(markup, dict)
            buttons = [
                b["text"]
                for row in markup.get("inline_keyboard", [])
                if isinstance(row, list)
                for b in row
                if isinstance(b, dict) and "text" in b
            ]
            self.assertIn("🆕 Gemini 3.8 Flash", buttons)
            self.assertIn("🔄 Обновить", buttons)
            service.state.close()


if __name__ == "__main__":
    unittest.main()
