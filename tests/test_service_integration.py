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
        self.markups: list[object | None] = []
        self.callbacks: list[tuple[str, str]] = []

    def send_html(self, chat_id: int, thread_id: int, text: str, **kwargs: object) -> None:
        self.sent.append((chat_id, thread_id, text))
        self.markups.append(kwargs.get("reply_markup"))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))


class FakeClient:
    def __init__(self) -> None:
        self.started = 0
        self.resumed = 0
        self.start_roots: list[Path] = []
        self.prompts: list[str] = []

    def start_thread(self, **kwargs: object) -> CodexThread:
        self.started += 1
        root = Path(str(kwargs["cwd"]))
        self.start_roots.append(root)
        return CodexThread(f"thread-{self.started}", root, "gpt-5.6-sol", "openai")

    def resume_thread(self, **_: object) -> CodexThread:
        self.resumed += 1
        return CodexThread("thread-1", Path.cwd(), "gpt-5.6-sol", "openai")

    def start_turn(self, **kwargs: object) -> str:
        self.prompts.append(str(kwargs["text"]))
        return "turn-1"

    def wait_for_turn(self, _: str) -> TurnResult:
        return TurnResult("Visible answer", 1000, 100)

    def read_rate_limits(self) -> RateLimits:
        return RateLimits(None, None)

    def list_models(self) -> list[dict[str, object]]:
        return [
            {
                "id": "gpt-5.6-sol",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "medium"},
                ],
            }
        ]

    def close(self) -> None:
        pass


class FakeSupervisor:
    def __init__(self, client: FakeClient) -> None:
        self.value = client
        self.client_calls = 0

    def client(self) -> FakeClient:
        self.client_calls += 1
        return self.value


class FakeExternalService:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.telegram = FakeTelegram()

    def handle_update(self, update: dict[str, object]) -> bool:
        self.updates.append(update)
        return True

    def close(self) -> None:
        pass


def update(
    message_id: int,
    text: str,
    *,
    chat_id: int = -1001234567890,
    thread_id: int = 77,
) -> dict[str, object]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "message_thread_id": thread_id,
            "is_topic_message": True,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": chat_id, "type": "supergroup", "title": "Private"},
            "text": text,
        },
    }


def callback(message_id: int, callback_id: str, data: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": 42, "is_bot": False},
            "data": data,
            "message": {
                "message_id": message_id,
                "message_thread_id": 77,
                "is_topic_message": True,
                "chat": {"id": -1001234567890, "type": "supergroup", "title": "Private"},
            },
        },
    }


def callback_values(markup: object) -> list[str]:
    if not isinstance(markup, dict):
        return []
    keyboard = markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return []
    return [
        str(button["callback_data"])
        for row in keyboard
        if isinstance(row, list)
        for button in row
        if isinstance(button, dict) and isinstance(button.get("callback_data"), str)
    ]


class ServiceIntegrationTests(unittest.TestCase):
    def test_model_command_cascades_provider_model_effort_before_applying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project_root = base / "Project"
            (project_root / ".git").mkdir(parents=True)
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=base / "projects.json",
                state_path=base / "state.db",
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
                    AgentDefinition(
                        "opencode",
                        "OpenCode",
                        "project_opencode_bot",
                        "opencode",
                        None,
                        False,
                        False,
                        "opencode-go/default",
                        "high",
                    ),
                ),
            )
            value = ProjectHubService.__new__(ProjectHubService)
            value.config = config
            value.registry = ProjectRegistry(
                1, (base,), (Project("project", "Project", "Project", project_root),)
            )
            value.state = HubState.open(config.state_path)
            value.agent = config.agents[0]
            telegram = FakeTelegram()
            value.telegram = cast(Any, telegram)
            value.supervisor = cast(Any, FakeSupervisor(FakeClient()))
            value._codex_client = None
            value.usernames = {"codex": "project_codex_bot"}

            self.assertTrue(value.handle_update(update(0, "/menu")))
            self.assertEqual(
                callback_values(telegram.markups[-1]),
                [
                    "menu:status",
                    "menu:model",
                    "menu:accounts",
                    "menu:new",
                    "menu:local",
                    "menu:return",
                ],
            )
            self.assertTrue(value.handle_update(callback(0, "cb-menu-status", "menu:status")))
            self.assertIn("No active agent session", telegram.sent[-1][2])

            self.assertTrue(value.handle_update(update(1, "/model")))
            self.assertIn("provider:codex", str(telegram.markups[-1]))
            self.assertTrue(value.handle_update(callback(2, "cb-provider", "provider:codex")))
            choose = callback_values(telegram.markups[-1])[0]
            self.assertRegex(choose, r"^choose:codex:[a-f0-9]{12}$")
            self.assertTrue(value.handle_update(callback(3, "cb-model", choose)))
            apply = next(
                item for item in callback_values(telegram.markups[-1]) if item.endswith(":medium")
            )
            self.assertRegex(apply, r"^use:codex:[a-f0-9]{12}:medium$")
            self.assertTrue(value.handle_update(callback(4, "cb-effort", apply)))
            topic = value.state.find_topic(-1001234567890, 77)
            assert topic is not None
            active = value.state.active_session(topic.topic_id)
            assert active is not None
            self.assertEqual(
                (active.agent_id, active.model, active.effort),
                ("codex", "gpt-5.6-sol", "medium"),
            )
            self.assertIn("will start on the next message", telegram.sent[-1][2])
            original_session_id = active.session_id
            self.assertTrue(value.handle_update(update(5, "/new")))
            confirm = next(
                item
                for item in callback_values(telegram.markups[-1])
                if item.startswith("new:confirm:")
            )
            unchanged = value.state.active_session(topic.topic_id)
            assert unchanged is not None
            self.assertEqual(unchanged.session_id, original_session_id)
            self.assertTrue(value.handle_update(callback(6, "cb-new", confirm)))
            replacement = value.state.active_session(topic.topic_id)
            assert replacement is not None
            self.assertNotEqual(replacement.session_id, original_session_id)
            self.assertTrue(value.handle_update(update(7, "/new unexpected")))
            self.assertIn("Usage: /new", telegram.sent[-1][2])

            external = FakeExternalService()
            value.external_services = cast(Any, {"opencode": external})
            value.state.activate_agent(topic.topic_id, "opencode", "opencode-go/default", "high")
            codex_message_count = len(telegram.sent)
            self.assertTrue(value.handle_update(update(8, "/status")))
            self.assertEqual(len(telegram.sent), codex_message_count)
            self.assertIn("OpenCode", external.telegram.sent[-1][2])
            value.state.close()

    def test_main_receives_unseen_satellite_dialogue_on_next_productive_turn(self) -> None:
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
                1, (base,), (Project("project", "Project", "Project", project_root),)
            )
            client = FakeClient()
            value = ProjectHubService.__new__(ProjectHubService)
            value.config = config
            value.registry = registry
            value.state = HubState.open(state_path)
            value.agent = config.agents[0]
            value.telegram = cast(Any, FakeTelegram())
            value.supervisor = cast(Any, FakeSupervisor(client))
            value._codex_client = None
            value.usernames = {"codex": "project_codex_bot"}
            topic = value.state.observe_topic(
                project_id="project", chat_id=-1001234567890, thread_id=77, title="Topic 77"
            )
            value.state.record_visible_turn(
                topic.topic_id,
                agent_id="antigravity",
                provider="antigravity",
                model="provider-selected",
                user_excerpt="relax, this is a connection test",
                response_excerpt="understood, connection works",
            )

            self.assertTrue(value.handle_update(update(8, "Now continue the project")))
            value.state.close()

        self.assertEqual(len(client.prompts), 1)
        self.assertIn("relax, this is a connection test", client.prompts[0])
        self.assertIn("understood, connection works", client.prompts[0])
        self.assertIn("Now continue the project", client.prompts[0])

    def test_central_ingress_dispatches_reply_to_external_agent_without_codex_turn(self) -> None:
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
                    AgentDefinition(
                        "antigravity",
                        "Antigravity",
                        "project_antigravity_bot",
                        "antigravity",
                        None,
                        False,
                        False,
                        "provider-selected",
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
            external = FakeExternalService()
            value = ProjectHubService.__new__(ProjectHubService)
            value.config = config
            value.registry = registry
            value.state = HubState.open(state_path)
            value.agent = config.agents[0]
            value.telegram = cast(Any, FakeTelegram())
            value.supervisor = cast(Any, FakeSupervisor(client))
            value._codex_client = None
            value.usernames = {
                "codex": "project_codex_bot",
                "antigravity": "project_antigravity_bot",
            }
            value.external_services = cast(Any, {"antigravity": external})
            incoming = update(4, "relax")
            cast(dict[str, Any], incoming["message"])["reply_to_message"] = {
                "message_id": 3,
                "from": {
                    "id": 8752263516,
                    "is_bot": True,
                    "username": "project_antigravity_bot",
                },
                "text": "previous response",
            }

            self.assertTrue(value.handle_update(incoming))
            value.state.close()

        self.assertEqual(external.updates, [incoming])
        self.assertEqual(client.started, 0)

    def test_authorized_unknown_group_is_discoverable_without_storing_message_text(self) -> None:
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
            value = ProjectHubService.__new__(ProjectHubService)
            value.config = config
            value.state = HubState.open(config.state_path)
            value.agent = config.agents[0]
            value.telegram = cast(Any, FakeTelegram())
            value.usernames = {"codex": "project_codex_bot"}
            unknown = update(9, "secret request text", chat_id=-1009999999999)
            cast(dict[str, Any], cast(dict[str, Any], unknown["message"])["chat"])["title"] = (
                "Example Project Beta\nforged"
            )

            self.assertFalse(value.handle_update(unknown))
            raw_events = value.state.status_snapshot()["runtime_events"]
            events = cast(list[dict[str, object]], raw_events)
            value.state.close()

        self.assertEqual(len(events), 1)
        detail = str(events[0]["detail"])
        self.assertIn("-1009999999999", detail)
        self.assertIn("Example Project Beta forged", detail)
        self.assertNotIn("secret request text", detail)

    def test_two_project_chats_create_isolated_topics_threads_and_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            roots = (base / "First", base / "Second")
            for root in roots:
                (root / ".git").mkdir(parents=True)
            state_path = base / "state.db"
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=base / "projects.json",
                state_path=state_path,
                codex_socket_path=base / "codex.sock",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(
                    ProjectBinding("first", -1001111111111),
                    ProjectBinding("second", -1002222222222),
                ),
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
                (
                    Project("first", "First", "First", roots[0]),
                    Project("second", "Second", "Second", roots[1]),
                ),
            )
            client = FakeClient()
            value = ProjectHubService.__new__(ProjectHubService)
            value.config = config
            value.registry = registry
            value.state = HubState.open(state_path)
            value.agent = config.agents[0]
            value.telegram = cast(Any, FakeTelegram())
            value.supervisor = cast(Any, FakeSupervisor(client))
            value._codex_client = None
            value.usernames = {"codex": "project_codex_bot"}

            self.assertTrue(
                value.handle_update(update(1, "first", chat_id=-1001111111111, thread_id=7))
            )
            self.assertTrue(
                value.handle_update(update(2, "second", chat_id=-1002222222222, thread_id=7))
            )
            first = value.state.find_topic(-1001111111111, 7)
            second = value.state.find_topic(-1002222222222, 7)
            assert first is not None and second is not None
            first_session = value.state.active_session(first.topic_id)
            second_session = value.state.active_session(second.topic_id)
            value.state.close()

        assert first_session is not None and second_session is not None
        self.assertNotEqual(first.topic_id, second.topic_id)
        self.assertNotEqual(first_session.provider_session_id, second_session.provider_session_id)
        self.assertEqual(client.start_roots, [root.resolve() for root in roots])

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

            supervisors: list[FakeSupervisor] = []

            def service() -> ProjectHubService:
                value = ProjectHubService.__new__(ProjectHubService)
                value.config = config
                value.registry = registry
                value.state = HubState.open(state_path)
                value.agent = config.agents[0]
                value.telegram = cast(Any, telegram)
                supervisor = FakeSupervisor(client)
                supervisors.append(supervisor)
                value.supervisor = cast(Any, supervisor)
                value._codex_client = None
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
        self.assertEqual([item.client_calls for item in supervisors], [1, 1])
        self.assertEqual(len(telegram.sent), 3)
        self.assertIn("Visible answer", telegram.sent[0][2])
        self.assertIn("Codex · GPT-5.6 Sol · High", telegram.sent[-1][2])
        self.assertIn("Context 90.0%", telegram.sent[-1][2])

    def test_local_takeover_blocks_telegram_and_returns_to_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project_root = base / "Project With Space"
            (project_root / ".git").mkdir(parents=True)
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=base / "projects.json",
                state_path=base / "state.db",
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
                1, (base,), (Project("project", "Project", "Project", project_root),)
            )
            client = FakeClient()
            telegram = FakeTelegram()
            value = ProjectHubService.__new__(ProjectHubService)
            value.config = config
            value.registry = registry
            value.state = HubState.open(config.state_path)
            value.agent = config.agents[0]
            value.telegram = cast(Any, telegram)
            value.supervisor = cast(Any, FakeSupervisor(client))
            value._codex_client = None
            value.usernames = {"codex": "project_codex_bot"}

            self.assertTrue(value.handle_update(update(10, "start")))
            self.assertTrue(value.handle_update(update(11, "/local")))
            topic = value.state.find_topic(-1001234567890, 77)
            assert topic is not None
            active = value.state.active_session(topic.topic_id)
            assert active is not None
            self.assertEqual(active.writer_mode, "local")
            self.assertIn("codex resume thread-1 -C", telegram.sent[-1][2])

            prompt_count = len(client.prompts)
            self.assertTrue(value.handle_update(update(12, "must not run")))
            self.assertEqual(len(client.prompts), prompt_count)
            self.assertIn("/return", telegram.sent[-1][2])

            self.assertTrue(value.handle_update(update(13, "/return")))
            active = value.state.active_session(topic.topic_id)
            assert active is not None
            self.assertEqual(active.writer_mode, "telegram")
            self.assertTrue(value.handle_update(update(14, "continue")))
            value.state.close()

        self.assertEqual(client.started, 1)
        self.assertEqual(client.resumed, 2)
        self.assertTrue(
            any(
                "Summarize only the work completed through the local CLI" in item
                for item in client.prompts
            )
        )


if __name__ == "__main__":
    unittest.main()
