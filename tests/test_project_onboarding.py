from __future__ import annotations

import asyncio
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.command_menu import GROUP_COMMANDS
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectBinding,
    ProjectProvisioningSettings,
    TerminalSettings,
)
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.project_editing import ProjectEditStore
from hermes_codex_router.project_onboarding import ProjectOnboardingStore
from hermes_codex_router.project_provisioner import (
    CreatedForum,
    ProjectProvisioner,
    ProvisioningRejected,
    ProvisioningUnknown,
)
from hermes_codex_router.registry import load_registry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramBotApi


class FakeProvisioningClient:
    def __init__(
        self,
        *,
        fail_create: bool = False,
        fail_preflight: bool = False,
        fail_configure: bool = False,
        identity_id: int = 42,
    ) -> None:
        self.fail_create = fail_create
        self.fail_preflight = fail_preflight
        self.fail_configure = fail_configure
        self.identity_id = identity_id
        self.create_calls = 0
        self.connect_calls = 0
        self.configured: tuple[CreatedForum, str, tuple[str, ...]] | None = None
        self.preflight: tuple[int, tuple[int, ...], str, tuple[str, ...]] | None = None
        self.closed = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def identity(self) -> int:
        return self.identity_id

    async def preflight_members(
        self,
        *,
        expected_creator_id: int,
        required_owner_ids: tuple[int, ...],
        hub_username: str,
        provider_usernames: tuple[str, ...],
        before_rpc: Any,
    ) -> None:
        before_rpc()
        if self.fail_preflight:
            raise ProvisioningUnknown("owner_entity_unavailable")
        self.preflight = (
            expected_creator_id,
            required_owner_ids,
            hub_username,
            provider_usernames,
        )

    async def create_private_forum(self, title: str, about: str) -> CreatedForum:
        self.create_calls += 1
        if self.fail_create:
            raise ProvisioningUnknown("network_timeout")
        self.title = title
        self.about = about
        return CreatedForum(-1001234567890, 987654321)

    async def configure_group(
        self,
        group: CreatedForum,
        *,
        hub_username: str,
        provider_usernames: tuple[str, ...],
        before_rpc: Any,
    ) -> None:
        before_rpc()
        if self.fail_configure:
            raise ProvisioningRejected("configuration_denied")
        self.configured = (group, hub_username, provider_usernames)

    async def close(self) -> None:
        self.closed += 1


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str, object | None]] = []
        self.callbacks: list[tuple[str, str]] = []
        self.commands: dict[str | None, list[dict[str, str]]] = {}
        self.calls: list[str] = []

    def send_html(self, chat_id: int, thread_id: int, text: str, **kwargs: object) -> int:
        self.sent.append((chat_id, thread_id, text, kwargs.get("reply_markup")))
        return 100 + len(self.sent)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))

    def call(self, method: str, **params: object) -> object:
        self.calls.append(method)
        scope = cast(str | None, params.get("scope"))
        if method == "setMyCommands":
            self.commands[scope] = cast(list[dict[str, str]], json.loads(str(params["commands"])))
            return True
        if method == "getMyCommands":
            return self.commands.get(scope, [])
        raise AssertionError(method)


def direct_update(message_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
    }


def direct_callback(message_id: int, data: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "callback_query": {
            "id": f"callback-{message_id}",
            "from": {"id": 42, "is_bot": False},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": 42, "type": "private"},
            },
        },
    }


def callback_values(markup: object | None) -> list[str]:
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


class ProjectOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.allowed = self.base / "projects"
        self.allowed.mkdir()
        self.registry_path = self.base / "projects.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.allowed)],
                    "projects": [],
                }
            ),
            encoding="utf-8",
        )
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=self.registry_path,
            state_path=self.base / "state.db",
            codex_socket_path=self.base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-5.6-sol",
                    "high",
                ),
                AgentDefinition(
                    "hermes",
                    "Hermes",
                    "example_hermes_bot",
                    "hermes",
                    None,
                    False,
                    True,
                    "provider-selected",
                    "high",
                ),
            ),
            hub_bot=HubTelegramBot("example_hub_bot", self.base / "hub-token"),
            project_provisioning=ProjectProvisioningSettings(
                True,
                12345,
                self.base / "api-hash",
                self.base / "owner.session",
                42,
                "Example Hub project",
            ),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def prepare_workflow(self, *, required_owner_ids: tuple[int, ...] | None = None) -> str:
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            workflow = store.start(owner_user_id=42, allowed_roots=(self.allowed,))
            workflow = store.set_name(42, workflow.workflow_id, "Example Project")
            option = store.options(workflow.workflow_id)[0]
            workflow = store.select_root(42, option.option_id)
            workflow = store.set_folder(42, workflow.workflow_id, "example-project")
            self.assertEqual(workflow.stage, "confirming")
            self.assertEqual(Path(str(workflow.canonical_root)), self.allowed / "example-project")
            first = store.confirm(
                42, workflow.workflow_id, required_owner_user_ids=required_owner_ids
            )
            second = store.confirm(
                42, workflow.workflow_id, required_owner_user_ids=required_owner_ids
            )
            self.assertEqual(first.workflow_id, second.workflow_id)
            return workflow.workflow_id
        finally:
            state.close()

    def test_worker_creates_git_group_bot_access_registry_and_binding(self) -> None:
        workflow_id = self.prepare_workflow()
        client = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=client, worker_id="test-worker")
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()

        project = load_registry(self.registry_path).require_project("example-project")
        self.assertTrue((project.root / ".git").exists())
        self.assertEqual(project.display_name, "Example Project")
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(
            client.preflight,
            (42, (42,), "example_hub_bot", ("example_codex_bot",)),
        )
        self.assertEqual(
            client.configured,
            (
                CreatedForum(-1001234567890, 987654321),
                "example_hub_bot",
                ("example_codex_bot",),
            ),
        )
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            self.assertEqual(store.get(workflow_id).stage, "completed")
            health = state.get_runtime_health("project_provisioner", "project-group-provisioner")
            assert health is not None
            self.assertEqual(health.activity_state, "idle")
            self.assertIsNotNone(health.success_at)
            binding = store.binding_for_chat(-1001234567890)
            assert binding is not None
            self.assertEqual(binding.project_id, "example-project")
            outbox = store.claim_outbox("test-sender")
            assert outbox is not None
            self.assertIn("создана и подключена", outbox.telegram_html)
        finally:
            state.close()

        hub_api = FakeTelegram()
        codex_api = FakeTelegram()
        sender = TelegramOutboxSender(
            replace(
                self.config,
                dispatch_mode="queue",
                queue_runtime="external",
                outbox_runtime="external",
                external_worker_agent_ids=("codex",),
            ),
            telegram_bots=cast(Any, {"hub": hub_api, "codex": codex_api}),
        )
        try:
            for _ in range(4):
                self.assertTrue(sender.run_cycle())
        finally:
            sender.close()
        scope = '{"type":"chat","chat_id":-1001234567890}'
        self.assertEqual(
            [item["command"] for item in hub_api.commands[scope]],
            [item[0] for item in GROUP_COMMANDS],
        )
        self.assertEqual(codex_api.commands[scope], [])

    def test_completion_notice_429_is_durably_deferred_without_reprovisioning(self) -> None:
        workflow_id = self.prepare_workflow()
        client = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=client)
        try:
            self.assertTrue(worker.run_cycle())
            self.assertFalse(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(client.create_calls, 1)

        body = io.BytesIO(
            json.dumps({"ok": False, "error_code": 429, "parameters": {"retry_after": 60}}).encode()
        )

        def rate_limited_opener(request: object, *, timeout: float) -> object:
            del timeout
            raise urllib.error.HTTPError(
                getattr(request, "full_url", "https://example.invalid"),
                429,
                "rate limited",
                cast(Any, {}),
                body,
            )

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.state = HubState.open(self.config.state_path)
        controller.telegram = TelegramBotApi("123:example", opener=rate_limited_opener)
        try:
            self.assertTrue(controller.run_project_onboarding_outbox_cycle())
            row = controller.state._connection.execute(
                """SELECT status,available_at,telegram_message_id
                   FROM project_onboarding_outbox WHERE workflow_id=?""",
                (workflow_id,),
            ).fetchone()
            self.assertEqual(row["status"], "prepared")
            self.assertIsNone(row["telegram_message_id"])
            self.assertGreater(
                datetime.fromisoformat(str(row["available_at"])), datetime.now(timezone.utc)
            )
            self.assertEqual(
                ProjectOnboardingStore(controller.state).get(workflow_id).stage, "completed"
            )
        finally:
            controller.state.close()

        replacement = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=replacement)
        try:
            self.assertFalse(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(replacement.create_calls, 0)

    def test_completion_notice_without_real_message_id_becomes_unknown(self) -> None:
        workflow_id = self.prepare_workflow()
        worker = ProjectProvisioner(self.config, client=FakeProvisioningClient())
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()

        class MissingMessageIdTelegram(FakeTelegram):
            def send_html(self, chat_id: int, thread_id: int, text: str, **kwargs: object) -> int:
                del chat_id, thread_id, text, kwargs
                return 0

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.state = HubState.open(self.config.state_path)
        controller.telegram = MissingMessageIdTelegram()
        try:
            self.assertTrue(controller.run_project_onboarding_outbox_cycle())
            row = controller.state._connection.execute(
                """SELECT status,error_code,telegram_message_id
                   FROM project_onboarding_outbox WHERE workflow_id=?""",
                (workflow_id,),
            ).fetchone()
            self.assertEqual(
                (row["status"], row["error_code"], row["telegram_message_id"]),
                ("unknown", "telegram_message_id_invalid", None),
            )
            self.assertEqual(
                ProjectOnboardingStore(controller.state).get(workflow_id).stage, "completed"
            )
        finally:
            controller.state.close()

    def test_unknown_group_creation_is_not_retried(self) -> None:
        workflow_id = self.prepare_workflow()
        client = FakeProvisioningClient(fail_create=True)
        worker = ProjectProvisioner(self.config, client=client, worker_id="test-worker")
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        second = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=second, worker_id="test-worker-2")
        try:
            self.assertFalse(worker.run_cycle())
        finally:
            worker.close()
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            self.assertEqual(store.get(workflow_id).stage, "group_unknown")
            with self.assertRaises(Exception):
                store.reconcile_unknown(
                    workflow_id,
                    telegram_chat_id=-1001234567890,
                    telegram_access_hash=987654321,
                    required_owner_user_ids=(42,),
                    confirm="wrong",
                )
            store.reconcile_unknown(
                workflow_id,
                telegram_chat_id=-1001234567890,
                telegram_access_hash=987654321,
                required_owner_user_ids=(42,),
                confirm=f"{workflow_id}:-1001234567890",
            )
        finally:
            state.close()
        third = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=third, worker_id="test-worker-3")
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(second.create_calls, 0)
        self.assertEqual(third.create_calls, 0)
        self.assertIsNotNone(third.configured)

    def test_worker_refuses_a_different_authorized_user(self) -> None:
        workflow_id = self.prepare_workflow()
        client = FakeProvisioningClient(identity_id=99)
        worker = ProjectProvisioner(self.config, client=client, worker_id="test-worker")
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(client.create_calls, 0)
        state = HubState.open(self.config.state_path)
        try:
            failed = ProjectOnboardingStore(state).get(workflow_id)
            self.assertEqual(failed.stage, "failed")
            self.assertEqual(failed.error_code, "project provisioning identity mismatch")
            self.assertEqual(failed.resume_stage, "preparing_root")
        finally:
            state.close()

    def test_two_owner_snapshot_is_preflighted_before_group_creation(self) -> None:
        workflow_id = self.prepare_workflow(required_owner_ids=(42, 43))
        client = FakeProvisioningClient()
        config = replace(self.config, owner_user_ids=(42, 43))
        worker = ProjectProvisioner(config, client=client)
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(client.create_calls, 1)
        assert client.preflight is not None
        self.assertEqual(client.preflight[:2], (42, (42, 43)))
        state = HubState.open(config.state_path)
        try:
            self.assertEqual(ProjectOnboardingStore(state).get(workflow_id).stage, "completed")
        finally:
            state.close()

    def test_preflight_failure_is_resumable_and_never_creates_a_group(self) -> None:
        workflow_id = self.prepare_workflow()
        blocked_client = FakeProvisioningClient(fail_preflight=True)
        worker = ProjectProvisioner(self.config, client=blocked_client)
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(blocked_client.create_calls, 0)
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            blocked = store.get(workflow_id)
            self.assertEqual((blocked.stage, blocked.resume_stage), ("failed", "preparing_root"))
            store.resume_blocked(workflow_id, required_owner_user_ids=(42,), confirm=workflow_id)
        finally:
            state.close()
        resumed_client = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=resumed_client)
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(resumed_client.create_calls, 1)

    def test_blocked_configuration_keeps_project_reservation(self) -> None:
        workflow_id = self.prepare_workflow()
        blocked_client = FakeProvisioningClient(fail_configure=True)
        worker = ProjectProvisioner(self.config, client=blocked_client)
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(blocked_client.create_calls, 1)

        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            blocked = store.get(workflow_id)
            self.assertEqual((blocked.stage, blocked.resume_stage), ("failed", "configuring_group"))
            second = store.start(owner_user_id=42, allowed_roots=(self.allowed,))
            second = store.set_name(42, second.workflow_id, "Duplicate")
            second = store.select_root(42, store.options(second.workflow_id)[0].option_id)
            with self.assertRaisesRegex(Exception, "onboarding_project_exists"):
                store.set_folder(42, second.workflow_id, "example-project")
            store.resume_blocked(
                workflow_id,
                required_owner_user_ids=(42,),
                confirm=workflow_id,
            )
        finally:
            state.close()

        resumed = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=resumed)
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()
        self.assertEqual(resumed.create_calls, 0)
        state = HubState.open(self.config.state_path)
        try:
            self.assertEqual(ProjectOnboardingStore(state).get(workflow_id).stage, "completed")
        finally:
            state.close()

    def test_expired_confirmation_cannot_queue_external_work(self) -> None:
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            workflow = store.start(owner_user_id=42, allowed_roots=(self.allowed,))
            workflow = store.set_name(42, workflow.workflow_id, "Expired")
            workflow = store.select_root(42, store.options(workflow.workflow_id)[0].option_id)
            workflow = store.set_folder(42, workflow.workflow_id, "expired")
            state._connection.execute(
                "UPDATE project_onboarding_workflows SET expires_at=? WHERE workflow_id=?",
                ("2020-01-01T00:00:00+00:00", workflow.workflow_id),
            )
            state._connection.commit()
            with self.assertRaisesRegex(Exception, "onboarding_selection_stale"):
                store.confirm(42, workflow.workflow_id)
            self.assertEqual(store.get(workflow.workflow_id).stage, "expired")
        finally:
            state.close()

    def test_folder_cannot_escape_allowed_root(self) -> None:
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            workflow = store.start(owner_user_id=42, allowed_roots=(self.allowed,))
            with self.assertRaises(Exception):
                store.set_name(42, workflow.workflow_id, "Invisible\u200bName")
            workflow = store.set_name(42, workflow.workflow_id, "Example")
            workflow = store.select_root(42, store.options(workflow.workflow_id)[0].option_id)
            for value in ("../outside", "/tmp/outside", "two/parts"):
                with self.subTest(value=value), self.assertRaises(Exception):
                    store.set_folder(42, workflow.workflow_id, value)
        finally:
            state.close()

    def test_private_hub_wizard_queues_one_opaque_workflow_without_provider(self) -> None:
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = self.config
        controller.registry = load_registry(self.registry_path)
        controller.state = HubState.open(self.config.state_path)
        self.addCleanup(controller.state.close)
        controller.agent = self.config.agents[0]
        controller.telegram = FakeTelegram()
        controller.ingress_identity = "hub"
        controller.direct_messages_only = False

        self.assertTrue(controller.handle_update(direct_update(1, "/projects")))
        create = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value == "po:b:start"
        )
        self.assertTrue(controller.handle_update(direct_callback(2, create)))
        self.assertTrue(controller.handle_update(direct_update(3, "Новый проект")))
        root_choice = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value.startswith("po:r:")
        )
        self.assertNotIn(str(self.allowed), root_choice)
        self.assertTrue(controller.handle_update(direct_callback(4, root_choice)))
        self.assertTrue(controller.handle_update(direct_update(5, "new-project")))
        confirm = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value.startswith("po:ok:")
        )
        self.assertTrue(controller.handle_update(direct_callback(6, confirm)))

        active = ProjectOnboardingStore(controller.state).active_for_owner(42)
        assert active is not None
        self.assertEqual(active.stage, "queued")
        self.assertEqual(active.project_id, "new-project")
        self.assertFalse((self.allowed / "new-project").exists())

    @patch("hermes_codex_router.service.PROJECT_EDIT_ENABLED", True)
    def test_private_hub_project_edit_changes_only_local_display_name(self) -> None:
        root = self.allowed / "example-project"
        root.mkdir()
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.allowed)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example Project",
                            "topic_name": "Telegram Group Title",
                            "root": str(root),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        config = replace(
            self.config,
            projects=(ProjectBinding("example-project", -1001234567890),),
        )
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = load_registry(self.registry_path)
        controller.state = HubState.open(config.state_path)
        self.addCleanup(controller.state.close)
        controller.agent = config.agents[0]
        controller.telegram = FakeTelegram()
        controller.ingress_identity = "hub"
        controller.direct_messages_only = False

        self.assertTrue(controller.handle_update(direct_update(101, "/projects")))
        start = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value == "pe:b:start"
        )
        self.assertTrue(controller.handle_update(direct_callback(102, start)))
        project = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value.startswith("pe:p:")
        )
        self.assertTrue(controller.handle_update(direct_callback(103, project)))
        rename = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value.startswith("pe:n:")
        )
        self.assertTrue(controller.handle_update(direct_callback(104, rename)))
        self.assertTrue(controller.handle_update(direct_update(105, "Renamed Project")))
        confirm = next(
            value
            for value in callback_values(controller.telegram.sent[-1][3])
            if value.startswith("pe:ok:")
        )
        self.assertTrue(controller.handle_update(direct_callback(106, confirm)))

        edited = load_registry(self.registry_path).require_project("example-project")
        self.assertEqual(edited.display_name, "Renamed Project")
        self.assertEqual(edited.topic_name, "Telegram Group Title")
        self.assertEqual(edited.root, root)
        self.assertEqual(controller.telegram.calls, [])

    def test_schema_29_rollback_hides_and_rejects_project_editing(self) -> None:
        root = self.allowed / "example-project"
        root.mkdir()
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.allowed)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example Project",
                            "topic_name": "Telegram Group Title",
                            "root": str(root),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        config = replace(
            self.config,
            projects=(ProjectBinding("example-project", -1001234567890),),
        )
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = config
        controller.registry = load_registry(self.registry_path)
        controller.state = HubState.open(config.state_path)
        self.addCleanup(controller.state.close)
        controller.agent = config.agents[0]
        controller.telegram = FakeTelegram()
        controller.ingress_identity = "hub"
        controller.direct_messages_only = False

        self.assertTrue(controller.handle_update(direct_update(201, "/projects")))
        callbacks = callback_values(controller.telegram.sent[-1][3])
        self.assertFalse(any(value.startswith("pe:") for value in callbacks))
        self.assertTrue(controller.handle_update(direct_callback(202, "pe:b:start")))
        self.assertIn("недоступно", controller.telegram.callbacks[-1][1])
        count = controller.state._connection.execute(
            "SELECT COUNT(*) FROM project_edit_workflows"
        ).fetchone()[0]
        self.assertEqual(count, 0)

        edit = ProjectEditStore(controller.state, self.registry_path)
        workflow = edit.start(owner_user_id=42, project_ids=("example-project",))
        option = edit.project_options(workflow.workflow_id)[0]
        selected = edit.select_project(42, option.option_id)
        edit.choose_rename(42, selected.workflow_id)
        self.assertTrue(controller.handle_update(direct_update(203, "Must not rename")))
        self.assertEqual(edit.get(selected.workflow_id).stage, "cancelled")
        self.assertEqual(
            load_registry(self.registry_path).require_project("example-project").display_name,
            "Example Project",
        )

    def test_stale_network_boundary_becomes_unknown_instead_of_retryable(self) -> None:
        workflow_id = self.prepare_workflow()
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            leased = store.claim_next("crashed-worker")
            assert leased is not None and leased.lease_token is not None
            store.mark_root_ready(leased.workflow_id, leased.lease_token)
            with state._connection:
                state._connection.execute(
                    """UPDATE project_onboarding_workflows
                       SET lease_expires_at='2000-01-01T00:00:00+00:00'
                       WHERE workflow_id=?""",
                    (workflow_id,),
                )

            self.assertIsNone(store.claim_next("replacement-worker"))
            recovered = store.get(workflow_id)
            self.assertEqual(recovered.stage, "group_unknown")
            self.assertEqual(recovered.error_code, "worker_lost")
        finally:
            state.close()

    def test_only_one_provisioning_workflow_is_leased_at_a_time(self) -> None:
        first_id = self.prepare_workflow()
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            second = store.start(owner_user_id=42, allowed_roots=(self.allowed,))
            second = store.set_name(42, second.workflow_id, "Second Project")
            second = store.select_root(42, store.options(second.workflow_id)[0].option_id)
            second = store.set_folder(42, second.workflow_id, "second-project")
            store.confirm(42, second.workflow_id)

            first = store.claim_next("worker-one")
            assert first is not None
            self.assertEqual(first.workflow_id, first_id)
            self.assertIsNone(store.claim_next("worker-two"))
        finally:
            state.close()

    def test_lease_heartbeat_fences_a_second_worker_during_slow_create(self) -> None:
        workflow_id = self.prepare_workflow()
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            second = store.start(owner_user_id=43, allowed_roots=(self.allowed,))
            second = store.set_name(43, second.workflow_id, "Second Project")
            second = store.select_root(43, store.options(second.workflow_id)[0].option_id)
            second = store.set_folder(43, second.workflow_id, "second-project")
            store.confirm(43, second.workflow_id, required_owner_user_ids=(42, 43))
        finally:
            state.close()

        class SlowClient(FakeProvisioningClient):
            def __init__(self) -> None:
                super().__init__()
                self.create_started = asyncio.Event()
                self.release_create = asyncio.Event()

            async def create_private_forum(self, title: str, about: str) -> CreatedForum:
                self.create_calls += 1
                self.title = title
                self.about = about
                self.create_started.set()
                await self.release_create.wait()
                return CreatedForum(-1001234567890, 987654321)

        async def scenario() -> None:
            client = SlowClient()
            worker = ProjectProvisioner(self.config, client=client, worker_id="slow-worker")
            try:
                task = asyncio.create_task(worker.run_cycle_async())
                await client.create_started.wait()
                await asyncio.sleep(0.36)
                competing_state = HubState.open(self.config.state_path)
                try:
                    competing_store = ProjectOnboardingStore(competing_state)
                    self.assertIsNone(competing_store.claim_next("competing-worker"))
                    active = competing_store.connection.execute(
                        """SELECT lease_expires_at FROM project_onboarding_workflows
                           WHERE workflow_id=?""",
                        (workflow_id,),
                    ).fetchone()
                    assert active is not None and active["lease_expires_at"] is not None
                    self.assertGreater(
                        datetime.fromisoformat(str(active["lease_expires_at"])),
                        datetime.now(timezone.utc),
                    )
                finally:
                    competing_state.close()
                client.release_create.set()
                self.assertTrue(await task)
            finally:
                worker.close()

        with (
            patch(
                "hermes_codex_router.project_onboarding.WORKER_LEASE",
                timedelta(seconds=0.3),
            ),
            patch("hermes_codex_router.project_provisioner.LEASE_HEARTBEAT_SECONDS", 0.05),
        ):
            asyncio.run(scenario())

    def test_stop_before_first_telegram_rpc_releases_workflow_without_mutation(self) -> None:
        workflow_id = self.prepare_workflow()
        client = FakeProvisioningClient()
        worker = ProjectProvisioner(self.config, client=client)
        original_prepare = worker._prepare_root

        def prepare_then_stop(workflow: Any) -> Path:
            root = original_prepare(workflow)
            worker.request_stop()
            return root

        worker._prepare_root = prepare_then_stop  # type: ignore[method-assign]
        try:
            self.assertFalse(asyncio.run(worker.run_cycle_async()))
            self.assertEqual((client.connect_calls, client.create_calls), (0, 0))
            self.assertEqual(worker.store.get(workflow_id).stage, "queued")
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
