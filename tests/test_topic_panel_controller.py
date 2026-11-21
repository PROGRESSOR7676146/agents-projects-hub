"""Owner `/panel` commands and the «Обновить» press through the Controller (ADR 0110)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.hub_config import (
    AcceptanceActor,
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState
from hermes_codex_router.topic_panel_controller import (
    PANEL_REFRESHING,
    PANEL_STALE,
    PANEL_USAGE,
    try_handle_topic_panel_command,
)
from tests.git_fixtures import init_git_root
from tests.test_service_integration import FakeTelegram, persist_registry

CHAT = -1001234567890
HUB_BOT_ID = 9001
ACTOR = 77


def message_update(
    update_id: int, text: str, *, sender_id: int = 42, chat_id: int = CHAT, **extra: Any
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id,
        "message_thread_id": 7,
        "is_topic_message": True,
        "from": {"id": sender_id, "is_bot": False},
        "chat": {"id": chat_id, "type": "supergroup", "title": "Private"},
        "text": text,
    }
    message.update(extra)
    return {"update_id": update_id, "message": message}


def press(
    callback_id: str,
    data: str,
    *,
    message_id: int,
    sender_id: int = 42,
    author_id: int = HUB_BOT_ID,
) -> dict[str, object]:
    return {
        "update_id": 1000,
        "callback_query": {
            "id": callback_id,
            "from": {"id": sender_id, "is_bot": False},
            "data": data,
            "message": {
                "message_id": message_id,
                "message_thread_id": 7,
                "is_topic_message": True,
                "from": {"id": author_id, "is_bot": True, "username": "example_hub_bot"},
                "chat": {"id": CHAT, "type": "supergroup", "title": "Private"},
            },
        },
    }


class TopicPanelControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        root = base / "Project"
        init_git_root(root)
        token = base / "hub.token"
        token.write_text("fictional", encoding="utf-8")
        self.config = HubConfig(
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
            hub_bot=HubTelegramBot("example_hub_bot", token, HUB_BOT_ID),
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
            acceptance_actors=(AcceptanceActor(ACTOR, CHAT, 7),),
            topic_panel=True,
        )
        service = ProjectHubService.__new__(ProjectHubService)
        service.config = self.config
        service.registry = ProjectRegistry(
            1, (base,), (Project("project", "Project", "Project", root),)
        )
        persist_registry(self.config.registry_path, service.registry)
        service.state = HubState.open(self.config.state_path, codex_permission_profile=None)
        self.addCleanup(service.state.close)
        service.agent = self.config.agents[0]
        service.ingress_identity = "hub"
        self.telegram = FakeTelegram()
        service.telegram = cast(Any, self.telegram)
        service.usernames = {"codex": "project_codex_bot"}
        self.service = service
        self.state = service.state
        self.db = service.state._connection

    def panel(self) -> Any:
        return self.db.execute("SELECT * FROM topic_panels").fetchone()

    def jobs(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0])

    def handle(self, update: dict[str, object]) -> bool:
        return cast(Any, self.service).handle_update(update)

    def created_panel(self) -> tuple[int, int]:
        self.assertTrue(self.handle(message_update(1, "/panel")))
        self.db.execute("UPDATE topic_panels SET message_id=600")
        self.db.commit()
        return 600, int(self.panel()["instance"])

    def test_owner_panel_opens_one_instance_without_a_prompt_or_reply(self) -> None:
        self.assertTrue(self.handle(message_update(1, "/panel")))
        panel = self.panel()
        self.assertEqual((panel["chat_id"], panel["thread_id"]), (CHAT, 7))
        self.assertEqual((panel["instance"], panel["enabled"]), (1, 1))
        self.assertIsNone(panel["message_id"])
        self.assertEqual((self.jobs(), self.telegram.sent), (0, []))
        # A duplicate update is claimed once.
        self.assertFalse(self.handle(message_update(1, "/panel")))
        self.assertEqual(self.panel()["instance"], 1)
        self.assertTrue(self.handle(message_update(2, "/panel@example_hub_bot")))
        self.assertEqual(self.panel()["instance"], 2)

    def test_panel_off_stops_and_reports(self) -> None:
        self.assertTrue(self.handle(message_update(1, "/panel")))
        self.assertTrue(self.handle(message_update(2, "/panel off")))
        self.assertEqual((self.panel()["enabled"], self.panel()["instance"]), (0, 2))
        self.assertTrue(self.handle(message_update(3, "/panel off")))
        self.assertEqual(
            [text for _, _, text in self.telegram.sent],
            ["Панель остановлена и больше не обновляется.", "В этой теме нет открытой панели."],
        )
        self.assertEqual(self.jobs(), 0)

    def test_refusals_are_control_only(self) -> None:
        cases = (
            (message_update(1, "/panel now"), PANEL_USAGE),
            (
                message_update(2, "/panel", photo=[{"file_id": "f", "file_unique_id": "u"}]),
                PANEL_USAGE,
            ),
            (message_update(3, "/panel", sender_id=ACTOR), "Панель может открыть только владелец."),
        )
        for update, expected in cases:
            with self.subTest(expected=expected):
                self.assertTrue(self.handle(update))
                self.assertEqual(self.telegram.sent[-1][2], expected)
        self.assertIsNone(self.panel())
        self.assertEqual(self.jobs(), 0)

    def test_unbound_or_private_chat_is_refused(self) -> None:
        message = cast(Any, self.service)
        from hermes_codex_router.telegram import parse_direct_message, parse_topic_message

        private = parse_direct_message(
            {
                "update_id": 5,
                "message": {
                    "message_id": 5,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "text": "/panel",
                },
            }
        )
        unbound = parse_topic_message(message_update(6, "/panel", chat_id=-1009999999999))
        for item in (private, unbound):
            assert item is not None
            self.assertTrue(try_handle_topic_panel_command(message, item))
            self.assertEqual(self.telegram.sent[-1][2], "Панель доступна только в теме проекта.")
        self.assertIsNone(self.panel())

    def test_disabled_flag_and_forwards_keep_previous_behavior(self) -> None:
        from hermes_codex_router.telegram import parse_topic_message

        command = parse_topic_message(message_update(1, "/panel"))
        assert command is not None
        self.service.config = replace(self.config, topic_panel=False)
        self.assertIsNone(try_handle_topic_panel_command(self.service, command))
        self.service.config = self.config
        forwarded = replace(command, is_forwarded=True)
        self.assertIsNone(try_handle_topic_panel_command(self.service, forwarded))
        self.assertIsNone(self.panel())

    def test_only_hub_ingress_serves_and_another_bot_keeps_its_command(self) -> None:
        from hermes_codex_router.telegram import parse_topic_message

        command = parse_topic_message(message_update(1, "/panel"))
        foreign = parse_topic_message(message_update(2, "/panel@project_codex_bot"))
        assert command is not None and foreign is not None
        self.assertIsNone(try_handle_topic_panel_command(self.service, foreign))
        self.service.ingress_identity = "codex"
        self.assertFalse(try_handle_topic_panel_command(self.service, command))
        # Neither claimed the update: the Hub ingress still serves it once.
        self.assertFalse(self.state.message_already_observed(CHAT, 1))
        self.assertFalse(self.state.message_already_observed(CHAT, 2))
        self.assertIsNone(self.panel())
        self.service.ingress_identity = "hub"
        self.assertTrue(try_handle_topic_panel_command(self.service, command))
        self.assertEqual(self.panel()["instance"], 1)

    def test_refresh_press_is_bound_to_the_exact_current_panel(self) -> None:
        message_id, instance = self.created_panel()
        data = f"pn:r:{instance}"
        self.assertTrue(self.handle(press("cb-1", data, message_id=message_id)))
        self.assertEqual(self.telegram.callbacks[-1], ("cb-1", PANEL_REFRESHING))
        requested = self.panel()["refresh_requested_at"]
        self.assertIsNotNone(requested)
        self.assertEqual(self.panel()["next_due_at"], requested)
        # A second tap is a new query ID but no second request.
        self.assertTrue(self.handle(press("cb-2", data, message_id=message_id)))
        self.assertEqual(self.telegram.callbacks[-1], ("cb-2", PANEL_REFRESHING))
        self.assertEqual(self.panel()["refresh_requested_at"], requested)
        # A repeated query ID is claimed once.
        self.assertFalse(self.handle(press("cb-2", data, message_id=message_id)))
        self.assertEqual(self.jobs(), 0)

    def test_stale_foreign_or_malformed_presses_change_nothing(self) -> None:
        message_id, instance = self.created_panel()
        before = tuple(self.panel())
        cases = (
            press("cb-1", f"pn:r:{instance + 1}", message_id=message_id),
            press("cb-2", f"pn:r:{instance}", message_id=message_id + 1),
            press("cb-3", f"pn:r:{instance}", message_id=message_id, author_id=9002),
            press("cb-4", "pn:r:abc", message_id=message_id),
            press("cb-5", "pn:x:1", message_id=message_id),
        )
        for update in cases:
            self.assertTrue(self.handle(update))
            self.assertEqual(self.telegram.callbacks[-1][1], PANEL_STALE)
        self.assertTrue(
            self.handle(press("cb-6", f"pn:r:{instance}", message_id=message_id, sender_id=ACTOR))
        )
        self.assertEqual(self.telegram.callbacks[-1][1], "Обновить панель может только владелец")
        self.service.config = replace(self.config, topic_panel=False)
        self.assertTrue(self.handle(press("cb-7", f"pn:r:{instance}", message_id=message_id)))
        self.assertEqual(self.telegram.callbacks[-1][1], PANEL_STALE)
        self.assertEqual(tuple(self.panel()), before)


if __name__ == "__main__":
    unittest.main()
