from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.hub_config import (
    HubConfigError,
    load_controller_config,
    load_provider_service_config,
)
from hermes_codex_router.service import ProjectHubService, ServiceError
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError, TopicCallback


class FakeTelegram:
    def __init__(self, token: str = "") -> None:
        self.token = token
        self.sent: list[tuple[int, int, str, object | None]] = []
        self.callbacks: list[tuple[str, str]] = []

    def send_html(self, chat_id: int, thread_id: int, text: str, **kwargs: object) -> int:
        self.sent.append((chat_id, thread_id, text, kwargs.get("reply_markup")))
        return 1

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))


class ControllerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        project_root = self.base / "project"
        (project_root / ".git").mkdir(parents=True)
        self.registry = self.base / "projects.json"
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.base)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example Project",
                            "topic_name": "Example",
                            "root": str(project_root),
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.hub_token = self.base / "hub-token"
        self.hub_token.write_text("654321:hub-secret", encoding="utf-8")
        self.hub_token.chmod(0o600)
        self.codex_token = self.base / "missing-codex-token"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def config_path(self, *, hub: bool = True) -> Path:
        document: dict[str, object] = {
            "schema_version": 1,
            "owner_user_ids": [42],
            "registry_path": str(self.registry),
            "state_path": str(self.base / "state.db"),
            "projects": [{"project_id": "example-project", "telegram_chat_id": -1001234567890}],
            "agents": [
                {
                    "agent_id": "codex",
                    "display_name": "Codex",
                    "telegram_username": "example_codex_bot",
                    "runtime": "codex",
                    "token_file": str(self.codex_token),
                    "terminal_enabled": True,
                }
            ],
            "dispatch_mode": "queue",
            "queue_runtime": "external",
            "outbox_runtime": "external",
        }
        if hub:
            document["hub_bot"] = {
                "telegram_username": "example_hub_bot",
                "token_file": str(self.hub_token),
            }
        path = self.base / "hub.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_hub_is_controller_ingress_while_codex_remains_default_provider(self) -> None:
        config = load_controller_config(self.config_path())
        created: list[FakeTelegram] = []

        def api(token: str) -> FakeTelegram:
            value = FakeTelegram(token)
            created.append(value)
            return value

        with patch("hermes_codex_router.service.TelegramBotApi", side_effect=api):
            service = ProjectHubService(config)
        try:
            self.assertEqual([item.token for item in created], ["654321:hub-secret"])
            self.assertEqual(service.ingress_identity, "hub")
            self.assertEqual(service.agent.agent_id, "codex")
            self.assertIs(service.telegram, created[0])
        finally:
            service.close()

    def test_hub_rejects_embedded_provider_execution_in_controller(self) -> None:
        path = self.config_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["external_worker_agent_ids"] = ["codex"]
        document["agents"].append(
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "example_opencode_bot",
                "runtime": "opencode",
                "token_file": str(self.base / "missing-opencode-token"),
                "terminal_enabled": False,
            }
        )
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(HubConfigError, "isolated external worker for agent: opencode"):
            load_controller_config(path)

    def test_hub_handles_suffixed_control_command_and_keeps_codex_default(self) -> None:
        config = load_controller_config(self.config_path())
        with patch("hermes_codex_router.service.TelegramBotApi", FakeTelegram):
            service = ProjectHubService(config)
        try:
            self.assertTrue(
                service.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 1,
                            "message_thread_id": 77,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "/menu@example_hub_bot",
                        },
                    }
                )
            )
            telegram = cast(FakeTelegram, service.telegram)
            self.assertEqual(telegram.sent[-1][2], "Project controls")
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            self.assertIsNone(service.state.active_session(topic.topic_id))
        finally:
            service.close()

    def test_hub_records_callback_under_controller_identity(self) -> None:
        config = load_controller_config(self.config_path())
        with patch("hermes_codex_router.service.TelegramBotApi", FakeTelegram):
            service = ProjectHubService(config)
        try:
            self.assertTrue(
                service._handle_callback(
                    TopicCallback(
                        callback_id="hub-callback",
                        message_id=1,
                        chat_id=-1001234567890,
                        thread_id=77,
                        sender_id=42,
                        data="menu:status",
                    )
                )
            )
            observer = service.state._connection.execute(
                "SELECT observer_agent_id FROM observed_callbacks WHERE callback_id = ?",
                ("hub-callback",),
            ).fetchone()
            self.assertEqual(observer[0], "hub")
        finally:
            service.close()

    def test_hub_mention_routes_to_codex_without_becoming_provider_prompt_text(self) -> None:
        config = load_controller_config(self.config_path())
        with patch("hermes_codex_router.service.TelegramBotApi", FakeTelegram):
            service = ProjectHubService(config)
        try:
            self.assertTrue(
                service.handle_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 2,
                            "message_thread_id": 77,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "@example_hub_bot do the work",
                        },
                    }
                )
            )
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            jobs = service.state.provider_jobs_for_topic(topic.topic_id)
            self.assertEqual(len(jobs), 1)
            self.assertEqual((jobs[0].agent_id, jobs[0].payload_text), ("codex", "do the work"))
        finally:
            service.close()

    def test_hub_controller_refuses_to_open_codex_response_transport(self) -> None:
        config = load_controller_config(self.config_path())
        created: list[FakeTelegram] = []

        def api(token: str) -> FakeTelegram:
            value = FakeTelegram(token)
            created.append(value)
            return value

        with patch("hermes_codex_router.service.TelegramBotApi", side_effect=api):
            service = ProjectHubService(config)
            try:
                self.assertEqual([item.token for item in created], ["654321:hub-secret"])
                with self.assertRaisesRegex(ServiceError, "does not own"):
                    service._provider_telegram("codex")
                self.assertEqual([item.token for item in created], ["654321:hub-secret"])
            finally:
                service.close()

    def test_single_process_hub_opens_codex_response_transport_lazily(self) -> None:
        self.codex_token.write_text("123456:codex-secret", encoding="utf-8")
        self.codex_token.chmod(0o600)
        path = self.config_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["queue_runtime"] = "embedded"
        document["outbox_runtime"] = "controller"
        path.write_text(json.dumps(document), encoding="utf-8")
        config = load_controller_config(path)
        created: list[FakeTelegram] = []

        def api(token: str) -> FakeTelegram:
            value = FakeTelegram(token)
            created.append(value)
            return value

        with patch("hermes_codex_router.service.TelegramBotApi", side_effect=api):
            service = ProjectHubService(config)
            try:
                self.assertEqual([item.token for item in created], ["654321:hub-secret"])
                response_transport = service._provider_telegram("codex")
                self.assertIs(response_transport, created[-1])
                self.assertEqual(
                    [item.token for item in created],
                    ["654321:hub-secret", "123456:codex-secret"],
                )
            finally:
                service.close()

    def test_codex_direct_ingress_reads_only_codex_token_and_ignores_groups(self) -> None:
        self.codex_token.write_text("123456:codex-secret", encoding="utf-8")
        self.codex_token.chmod(0o600)
        config = load_provider_service_config(self.config_path(), "codex")
        created: list[FakeTelegram] = []

        def api(token: str) -> FakeTelegram:
            value = FakeTelegram(token)
            created.append(value)
            return value

        with patch("hermes_codex_router.service.TelegramBotApi", side_effect=api):
            service = ProjectHubService(config, ingress_identity="codex", direct_messages_only=True)
        try:
            self.assertEqual([item.token for item in created], ["123456:codex-secret"])
            self.assertEqual(service.ingress_identity, "codex")
            self.assertFalse(service._publishes_controller_health)
            self.assertFalse(
                service.handle_update(
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 3,
                            "message_thread_id": 77,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "must not be consumed by the direct endpoint",
                        },
                    }
                )
            )
        finally:
            service.close()

    def test_hub_mention_is_removed_from_external_provider_queue_prompt(self) -> None:
        path = self.config_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["external_worker_agent_ids"] = ["codex", "opencode"]
        document["agents"].append(
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "example_opencode_bot",
                "runtime": "opencode",
                "token_file": str(self.base / "missing-opencode-token"),
                "terminal_enabled": False,
            }
        )
        path.write_text(json.dumps(document), encoding="utf-8")
        config = load_controller_config(path)
        with patch("hermes_codex_router.service.TelegramBotApi", FakeTelegram):
            service = ProjectHubService(config)
        try:
            self.assertTrue(
                service.handle_update(
                    {
                        "update_id": 4,
                        "message": {
                            "message_id": 4,
                            "message_thread_id": 77,
                            "is_topic_message": True,
                            "from": {"id": 42, "is_bot": False},
                            "chat": {
                                "id": -1001234567890,
                                "type": "supergroup",
                                "title": "Example",
                            },
                            "text": "@example_hub_bot @example_opencode_bot inspect this",
                        },
                    }
                )
            )
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            jobs = service.state.provider_jobs_for_topic(topic.topic_id)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].agent_id, "opencode")
            self.assertEqual(jobs[0].payload_text, "inspect this")
        finally:
            service.close()

    def test_polling_persists_hub_offset_separately_from_codex(self) -> None:
        class State:
            def __init__(self) -> None:
                self.requested: list[str] = []
                self.saved: list[tuple[str, int]] = []

            def record_runtime_event(self, *_args: object) -> None:
                pass

            def get_bot_offset(self, identity: str) -> None:
                self.requested.append(identity)
                return None

            def set_bot_offset(self, identity: str, offset: int) -> None:
                self.saved.append((identity, offset))

        class Telegram:
            def __init__(self) -> None:
                self.polls = 0

            def updates(self, **_kwargs: object) -> list[dict[str, int]]:
                self.polls += 1
                if self.polls == 1:
                    return [{"update_id": 9}]
                raise KeyboardInterrupt

        service = cast(Any, ProjectHubService.__new__(ProjectHubService))
        service._stop = threading.Event()
        service.supervisor = None
        service.ingress_identity = "hub"
        service.state = State()
        service.telegram = Telegram()
        service.handle_update = lambda _update: True
        service._start_embedded_queue_consumer = lambda: None
        service._start_controller_outbox_delivery = lambda: None

        with self.assertRaises(KeyboardInterrupt):
            service.run_forever()

        self.assertEqual(service.state.requested, ["hub"])
        self.assertEqual(service.state.saved, [("hub", 10)])

    def test_controller_poll_transport_health_threshold_recovers_and_rearms(self) -> None:
        service = cast(Any, ProjectHubService.__new__(ProjectHubService))
        service.state = HubState.open(self.base / "transport.db")
        service._publishes_controller_health = True
        service._health_started_at = datetime.now(timezone.utc)
        service._health_process_start_marker = "controller-transport-test"
        service._health_last_success_at = None
        service._health_last_error_code = None
        service._health_transport_error = None
        service._health_transport_consecutive_failures = 0
        service._health_transport_success_at = None
        service._health_transport_reported_signature = None
        service._health_last_publish_monotonic = 0.0
        error = TelegramError(
            "safe failure",
            operation="poll",
            failure_class="api_http",
            status_code=502,
        )
        try:
            service._record_telegram_poll_failure("hub", error)
            service._record_telegram_poll_failure("hub", error)
            service._publish_runtime_health(force=True)
            transient = service.state.get_runtime_health("controller", "project-hub-controller")
            assert transient is not None
            self.assertIsNone(transient.error_code)
            self.assertEqual(transient.transport_consecutive_failures, 2)
            self.assertEqual(
                service.state.runtime_health_status("controller", "project-hub-controller").status,
                "healthy",
            )
            self.assertEqual(service.state.status_snapshot()["runtime_events"], [])

            service._record_telegram_poll_failure("hub", error)
            service._publish_runtime_health(force=True)
            failed = service.state.get_runtime_health("controller", "project-hub-controller")
            assert failed is not None
            self.assertEqual(failed.transport_operation, "poll")
            self.assertEqual(failed.transport_failure_class, "api_http")
            self.assertEqual(failed.transport_status_code, 502)
            self.assertEqual(failed.transport_consecutive_failures, 3)
            self.assertEqual(
                service.state.runtime_health_status("controller", "project-hub-controller").status,
                "degraded",
            )

            service._record_telegram_poll_success("hub")
            service._publish_runtime_health(force=True)
            recovered = service.state.get_runtime_health("controller", "project-hub-controller")
            assert recovered is not None
            self.assertIsNone(recovered.transport_operation)
            self.assertEqual(recovered.transport_consecutive_failures, 0)
            events = cast(
                list[dict[str, object]],
                service.state.status_snapshot()["runtime_events"],
            )
            self.assertEqual(
                [event["code"] for event in reversed(events)],
                ["telegram_transport_error", "telegram_recovered"],
            )

            for _ in range(3):
                service._record_telegram_poll_failure("hub", error)
            service._record_telegram_poll_success("hub")
            events = cast(
                list[dict[str, object]],
                service.state.status_snapshot()["runtime_events"],
            )
            self.assertEqual(
                [event["code"] for event in reversed(events)],
                [
                    "telegram_transport_error",
                    "telegram_recovered",
                    "telegram_transport_error",
                    "telegram_recovered",
                ],
            )
        finally:
            service.state.close()

    def test_hub_offset_does_not_replace_existing_codex_offset(self) -> None:
        state = HubState.open(self.base / "offsets.db")
        try:
            state.set_bot_offset("codex", 17)
            state.set_bot_offset("hub", 31)
            self.assertEqual(state.get_bot_offset("codex"), 17)
            self.assertEqual(state.get_bot_offset("hub"), 31)
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
