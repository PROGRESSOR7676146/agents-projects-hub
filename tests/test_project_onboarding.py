from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectProvisioningSettings,
    TerminalSettings,
)
from hermes_codex_router.project_onboarding import ProjectOnboardingStore
from hermes_codex_router.project_provisioner import (
    CreatedForum,
    ProjectProvisioner,
    ProvisioningUnknown,
)
from hermes_codex_router.registry import load_registry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState


class FakeProvisioningClient:
    def __init__(self, *, fail_create: bool = False, identity_id: int = 42) -> None:
        self.fail_create = fail_create
        self.identity_id = identity_id
        self.create_calls = 0
        self.configured: tuple[CreatedForum, str, tuple[str, ...]] | None = None
        self.closed = 0

    async def connect(self) -> None:
        return None

    async def identity(self) -> int:
        return self.identity_id

    async def create_private_forum(self, title: str, about: str) -> CreatedForum:
        self.create_calls += 1
        if self.fail_create:
            raise ProvisioningUnknown("network_timeout")
        self.title = title
        self.about = about
        return CreatedForum(-1001234567890, 987654321)

    async def configure_bots(
        self,
        group: CreatedForum,
        *,
        hub_username: str,
        provider_usernames: tuple[str, ...],
    ) -> None:
        self.configured = (group, hub_username, provider_usernames)

    async def close(self) -> None:
        self.closed += 1


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str, object | None]] = []
        self.callbacks: list[tuple[str, str]] = []

    def send_html(self, chat_id: int, thread_id: int, text: str, **kwargs: object) -> int:
        self.sent.append((chat_id, thread_id, text, kwargs.get("reply_markup")))
        return 100 + len(self.sent)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))


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

    def prepare_workflow(self) -> str:
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
            first = store.confirm(42, workflow.workflow_id)
            second = store.confirm(42, workflow.workflow_id)
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
            client.configured,
            (
                CreatedForum(-1001234567890, 987654321),
                "example_hub_bot",
                ("example_codex_bot", "example_hermes_bot"),
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
                    confirm="wrong",
                )
            store.reconcile_unknown(
                workflow_id,
                telegram_chat_id=-1001234567890,
                telegram_access_hash=987654321,
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
            self.assertEqual(failed.error_code, "ProjectProvisioningError")
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


if __name__ == "__main__":
    unittest.main()
