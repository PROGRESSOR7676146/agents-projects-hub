from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from hermes_codex_router.acceptance_actor import (
    P0_P1_CHECKS,
    AcceptanceActorConfig,
    AcceptanceActorError,
    AcceptanceCheckResult,
    _click_callback_exact,
    _forward_to_topic,
    _run_check,
    _run_configured_checks,
    _run_p0_p1_live_checks,
    _targets_for_check,
    _wait_for_response,
    load_acceptance_actor_config,
)
from hermes_codex_router.acceptance_contracts import (
    AcceptanceActorConfig as ContractConfig,
)
from hermes_codex_router.acceptance_contracts import (
    AcceptanceActorError as ContractError,
)
from hermes_codex_router.acceptance_contracts import (
    AcceptanceCheckResult as ContractResult,
)
from hermes_codex_router.acceptance_runtime import (
    AcceptanceRuntimeError,
    FixedServiceSupervisor,
    ReadOnlyAcceptanceState,
    ServiceSnapshot,
)
from hermes_codex_router.p0_p1_acceptance import (
    P0P1ScenarioContext,
    run_p0_p1_live_scenario,
)


class FakeButton:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.clicked = False

    async def click(self) -> None:
        self.clicked = True


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        text: str = "",
        button: FakeButton | None = None,
    ) -> None:
        self.id = message_id
        self.raw_text = text
        self.buttons = [[button]] if button is not None else None
        self.grouped_id: int | None = None


class FakeDocumentMessage(FakeMessage):
    def __init__(self, message_id: int, name: str, payload: bytes) -> None:
        super().__init__(message_id, "Done")
        self.document = object()
        self.file = SimpleNamespace(name=name)
        self.payload = payload

    async def download_media(self, *, file: object) -> bytes:
        assert file is bytes
        return self.payload


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send_message(self, *_args: object, **_kwargs: object) -> FakeMessage:
        self.sent.append((_args, _kwargs))
        return FakeMessage(len(self.sent))


class FakeP0P1Client(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.uploaded_payloads: list[bytes] = []
        self._next_id = 100

    async def send_message(self, *_args: object, **_kwargs: object) -> FakeMessage:
        self.sent.append((_args, _kwargs))
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def send_file(self, _chat_id: int, file: object, **_kwargs: object) -> object:
        paths = file if isinstance(file, list) else [file]
        messages = []
        grouped_id = 9876 if isinstance(file, list) else None
        for value in paths:
            self.uploaded_payloads.append(Path(str(value)).read_bytes())
            self._next_id += 1
            message = FakeMessage(self._next_id)
            message.grouped_id = grouped_id
            messages.append(message)
        return messages if isinstance(file, list) else messages[0]


class StatefulAcceptanceProbe(ReadOnlyAcceptanceState):
    def __init__(self, *, fail_message_id: int | None = None) -> None:
        self.fail_message_id = fail_message_id
        self.calls: list[tuple[str, object]] = []
        self.jobs = {
            101: [[("caption", "completed")]],
            102: [[("album", "completed")]],
            103: [[("album", "completed")]],
            104: [[("active", "executing")]],
            105: [[("late", "queued")], [("late", "completed")]],
            106: [
                [("recovery", "queued")],
                [("recovery", "queued")],
                [("recovery", "completed")],
            ],
        }

    def jobs_for_input(self, chat_id: int, message_id: int) -> list[tuple[str, str]]:
        self.calls.append(("jobs", message_id))
        if message_id == self.fail_message_id:
            raise AcceptanceRuntimeError("named state probe failure")
        states = self.jobs[message_id]
        return states.pop(0) if len(states) > 1 else states[0]

    def material_count(self, job_id: str) -> int:
        self.calls.append(("materials", job_id))
        return {"album": 2, "recovery": 1}[job_id]


class StatefulServiceSupervisor(FixedServiceSupervisor):
    def __init__(
        self,
        *,
        controller_active: bool = True,
        worker_active: bool = True,
        fail_action: str | None = None,
        fail_restore: bool = False,
    ) -> None:
        self.controller_active = controller_active
        self.worker_active = worker_active
        self.fail_action = fail_action
        self.fail_restore = fail_restore
        self.actions: list[str] = []

    def capture_active_state(self) -> ServiceSnapshot:
        self.actions.append("capture")
        return ServiceSnapshot(self.controller_active, self.worker_active)

    def stop_codex_worker(self) -> None:
        self.actions.append("stop_worker")
        self.worker_active = False

    def restart_controller(self) -> None:
        self.actions.append("restart_controller")
        if self.fail_action == "restart_controller":
            self.controller_active = False
            raise AcceptanceRuntimeError("named Controller restart failure")
        self.controller_active = True

    def start_codex_worker(self) -> None:
        self.actions.append("start_worker")
        self.worker_active = True

    def is_controller_active(self) -> bool:
        self.actions.append("check_controller")
        return self.controller_active

    def is_codex_worker_active(self) -> bool:
        self.actions.append("check_worker")
        return self.worker_active

    def restore(self, initial: ServiceSnapshot) -> None:
        self.actions.append("restore")
        if initial.controller_active:
            self.controller_active = True
        if initial.codex_worker_active:
            self.worker_active = True
        if self.fail_restore:
            raise AcceptanceRuntimeError("named restoration failure")


class FakeRawClient:
    def __init__(self) -> None:
        self.request: object | None = None

    async def get_input_entity(self, entity: object) -> str:
        return f"peer:{entity}"

    async def __call__(self, request: object) -> object:
        self.request = request
        message = SimpleNamespace(id=91)
        return SimpleNamespace(updates=[SimpleNamespace(message=message)])


class FakeIncomingMessage(FakeMessage):
    def __init__(self, message_id: int, username: str, *, sender_id: int = 2) -> None:
        super().__init__(message_id, "message")
        self.reply_to = SimpleNamespace(reply_to_top_id=77, reply_to_msg_id=77)
        self._sender = SimpleNamespace(id=sender_id, username=username)

    async def get_sender(self) -> object:
        return self._sender


class FakeIterClient:
    def __init__(self, messages: list[FakeIncomingMessage]) -> None:
        self.messages = messages

    async def iter_messages(self, *_args: object, **_kwargs: object):
        for message in self.messages:
            yield message


class AcceptanceActorConfigTests(unittest.TestCase):
    def test_exact_callback_accepts_generation_bound_control(self) -> None:
        button = FakeButton(b"provider:codex~0123456789abcdef")
        message = FakeMessage(1, button=button)
        asyncio.run(_click_callback_exact(message, b"provider:codex"))
        self.assertTrue(button.clicked)

    def test_exact_callback_does_not_select_prefix_collision(self) -> None:
        button = FakeButton(b"provider:codex-extra~0123456789abcdef")
        with self.assertRaises(AcceptanceActorError):
            asyncio.run(_click_callback_exact(FakeMessage(1, button=button), b"provider:codex"))
        self.assertFalse(button.clicked)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.secret = self.base / "telegram-api-hash"
        self.secret.touch()
        self.secret.chmod(0o600)
        self.api_hash_reader = patch(
            "hermes_codex_router.acceptance_actor._read_api_hash",
            return_value="0" * 32,
        )
        self.api_hash_reader.start()
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.api_hash_reader.stop()
        self.tempdir.cleanup()

    def write_config(self, **overrides: object) -> Path:
        document = {
            "schema_version": 1,
            "api_id": 12345,
            "session_path": str(self.base / "acceptance.session"),
            "expected_user_id": 987654321,
            "telegram_chat_id": -1001234567890,
            "telegram_thread_id": 77,
            "hub_username": "example_hub_bot",
            "provider_usernames": ["example_provider_bot"],
            "checks": [
                "status",
                "accounts",
                "model_menu",
                "provider_ping",
                "reply_route",
            ],
            "timeout_seconds": 15,
            "artifacts_dir": str(self.artifacts),
        }
        document.update(overrides)
        path = self.base / "actor.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        return path

    def p0_config(self, state_path: Path) -> AcceptanceActorConfig:
        return AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_codex_bot",),
            checks=("p0_p1_live",),
            timeout_seconds=180,
            artifacts_dir=self.artifacts,
            provider_agent_ids=("codex",),
            state_path=state_path,
            allow_service_restart=True,
        )

    def test_loads_private_scoped_config(self) -> None:
        config = load_acceptance_actor_config(self.write_config())

        self.assertEqual(config.telegram_thread_id, 77)
        self.assertEqual(
            config.checks,
            ("status", "accounts", "model_menu", "provider_ping", "reply_route"),
        )
        self.assertEqual(config.provider_usernames, ("example_provider_bot",))

    def test_rejects_world_readable_config_or_secret(self) -> None:
        path = self.write_config()
        path.chmod(0o644)
        with self.assertRaisesRegex(AcceptanceActorError, "mode 0600"):
            load_acceptance_actor_config(path)

        path.chmod(0o600)
        self.secret.chmod(0o644)
        with self.assertRaisesRegex(AcceptanceActorError, "mode 0600"):
            load_acceptance_actor_config(path)

    def test_rejects_unknown_checks_and_general_topic(self) -> None:
        with self.assertRaisesRegex(AcceptanceActorError, "checks"):
            load_acceptance_actor_config(self.write_config(checks=["arbitrary_command"]))
        with self.assertRaisesRegex(AcceptanceActorError, "dedicated forum topic"):
            load_acceptance_actor_config(self.write_config(telegram_thread_id=1))

    def test_rejects_malformed_api_hash_content(self) -> None:
        with patch(
            "hermes_codex_router.acceptance_actor._read_api_hash",
            return_value="not-an-api-hash",
        ):
            with self.assertRaisesRegex(AcceptanceActorError, "API hash"):
                load_acceptance_actor_config(self.write_config())

    def test_rejects_api_hash_or_path_embedded_in_config(self) -> None:
        for key in ("api_hash", "api_hash_file"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(AcceptanceActorError, "sibling"):
                    load_acceptance_actor_config(self.write_config(**{key: "forbidden"}))

    def test_provider_ping_requires_a_provider_allowlist(self) -> None:
        with self.assertRaisesRegex(AcceptanceActorError, "provider_usernames"):
            load_acceptance_actor_config(self.write_config(provider_usernames=[]))

    def test_reply_route_requires_a_provider_allowlist(self) -> None:
        with self.assertRaisesRegex(AcceptanceActorError, "provider_usernames"):
            load_acceptance_actor_config(
                self.write_config(checks=["reply_route"], provider_usernames=[])
            )

    def test_burst_and_stop_routes_require_a_provider_allowlist(self) -> None:
        for check in ("burst_route", "stop_route", "forwarded_quote"):
            with (
                self.subTest(check=check),
                self.assertRaisesRegex(AcceptanceActorError, "provider_usernames"),
            ):
                load_acceptance_actor_config(
                    self.write_config(checks=[check], provider_usernames=[])
                )

    def test_stop_route_requires_model_selection_first(self) -> None:
        with self.assertRaisesRegex(AcceptanceActorError, "model_menu must run before"):
            load_acceptance_actor_config(self.write_config(checks=["stop_route", "model_menu"]))

    def test_context_contract_requires_two_aligned_provider_identities(self) -> None:
        with self.assertRaisesRegex(AcceptanceActorError, "two aligned providers"):
            load_acceptance_actor_config(
                self.write_config(checks=["context_contract"], provider_agent_ids=[])
            )
        with self.assertRaisesRegex(AcceptanceActorError, "must align"):
            load_acceptance_actor_config(
                self.write_config(
                    provider_usernames=["first_provider_bot", "second_provider_bot"],
                    provider_agent_ids=["first"],
                )
            )

    def test_codex_interaction_v2_requires_one_aligned_codex_identity(self) -> None:
        with self.assertRaisesRegex(AcceptanceActorError, "aligned codex provider"):
            load_acceptance_actor_config(
                self.write_config(
                    checks=["codex_interaction_v2"],
                    provider_agent_ids=["opencode"],
                )
            )

    def test_codex_interaction_v2_targets_only_aligned_codex_provider(self) -> None:
        config = load_acceptance_actor_config(
            self.write_config(
                provider_usernames=["example_other_bot", "example_codex_bot"],
                provider_agent_ids=["opencode", "codex"],
                checks=["codex_interaction_v2"],
            )
        )

        self.assertEqual(
            _targets_for_check(config, "codex_interaction_v2"),
            ("example_codex_bot",),
        )

    def test_p0_p1_live_requires_private_state_restart_opt_in_and_codex(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        with self.assertRaisesRegex(AcceptanceActorError, "state_path"):
            load_acceptance_actor_config(
                self.write_config(
                    checks=["p0_p1_live"],
                    provider_agent_ids=["codex"],
                    allow_service_restart=True,
                    timeout_seconds=180,
                )
            )
        with self.assertRaisesRegex(AcceptanceActorError, "service restart"):
            load_acceptance_actor_config(
                self.write_config(
                    checks=["p0_p1_live"],
                    provider_agent_ids=["codex"],
                    state_path=str(state_path),
                    timeout_seconds=180,
                )
            )
        with self.assertRaisesRegex(AcceptanceActorError, "aligned codex provider"):
            load_acceptance_actor_config(
                self.write_config(
                    checks=["p0_p1_live"],
                    provider_agent_ids=["opencode"],
                    state_path=str(state_path),
                    allow_service_restart=True,
                    timeout_seconds=180,
                )
            )

        config = load_acceptance_actor_config(
            self.write_config(
                checks=["p0_p1_live"],
                provider_agent_ids=["codex"],
                state_path=str(state_path),
                allow_service_restart=True,
                timeout_seconds=180,
            )
        )

        self.assertEqual(config.state_path, state_path)
        self.assertTrue(config.allow_service_restart)
        self.assertEqual(_targets_for_check(config, "p0_p1_live"), ("example_provider_bot",))

    def test_p0_p1_live_accepts_the_recommended_360_second_timeout(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)

        config = load_acceptance_actor_config(
            self.write_config(
                checks=["p0_p1_live"],
                provider_agent_ids=["codex"],
                state_path=str(state_path),
                allow_service_restart=True,
                timeout_seconds=360,
            )
        )

        self.assertEqual(config.timeout_seconds, 360)

    def test_stop_route_targets_only_the_provider_selected_by_model_menu(self) -> None:
        config = load_acceptance_actor_config(
            self.write_config(
                provider_usernames=["first_provider_bot", "second_provider_bot"],
                checks=["model_menu", "stop_route"],
            )
        )

        self.assertEqual(_targets_for_check(config, "stop_route"), ("first_provider_bot",))
        self.assertEqual(
            _targets_for_check(config, "provider_ping"),
            ("first_provider_bot", "second_provider_bot"),
        )

    def test_login_bootstrap_may_load_without_expected_identity(self) -> None:
        path = self.write_config()
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["expected_user_id"]
        path.write_text(json.dumps(document), encoding="utf-8")

        config = load_acceptance_actor_config(path, require_identity=False)
        self.assertIsNone(config.expected_user_id)
        with self.assertRaisesRegex(AcceptanceActorError, "expected_user_id"):
            load_acceptance_actor_config(path)

    def test_model_menu_check_clicks_provider_model_and_effort(self) -> None:
        provider = FakeButton(b"provider:codex")
        model = FakeButton(b"choose:codex:model")
        effort = FakeButton(b"use:codex:model:high")
        responses = (
            FakeMessage(2, button=provider),
            FakeMessage(3, button=model),
            FakeMessage(4, button=effort),
            FakeMessage(5, "Codex will start on the next message."),
        )
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("model_menu",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(side_effect=responses),
        ):
            result = asyncio.run(
                _run_check(FakeClient(), config, "model_menu", config.hub_username)
            )

        self.assertTrue(result.ok)
        self.assertTrue(all(button.clicked for button in (provider, model, effort)))

    def test_model_menu_accepts_current_provider_activation_response(self) -> None:
        provider = FakeButton(b"provider:codex")
        model = FakeButton(b"choose:codex:model")
        effort = FakeButton(b"use:codex:model:high")
        responses = (
            FakeMessage(2, button=provider),
            FakeMessage(3, button=model),
            FakeMessage(4, button=effort),
            FakeMessage(5, "Codex is now active (generation 2)."),
        )
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("model_menu",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(side_effect=responses),
        ):
            result = asyncio.run(
                _run_check(FakeClient(), config, "model_menu", config.hub_username)
            )

        self.assertTrue(all(button.clicked for button in (provider, model, effort)))
        self.assertTrue(result.ok)

    def test_wait_for_response_fails_fast_on_unrelated_canary_traffic(self) -> None:
        config = load_acceptance_actor_config(self.write_config())
        client = FakeIterClient([FakeIncomingMessage(2, "unrelated_user", sender_id=42)])

        with self.assertRaisesRegex(AcceptanceActorError, "unrelated traffic"):
            asyncio.run(
                _wait_for_response(
                    client,
                    config,
                    after_id=1,
                    username="example_provider_bot",
                )
            )

    def test_wait_for_response_allows_actor_and_configured_bot_senders(self) -> None:
        config = load_acceptance_actor_config(self.write_config())
        response = FakeIncomingMessage(3, "example_provider_bot")
        client = FakeIterClient(
            [
                FakeIncomingMessage(2, "acceptance_actor", sender_id=987654321),
                response,
            ]
        )

        received = asyncio.run(
            _wait_for_response(
                client,
                config,
                after_id=1,
                username="example_provider_bot",
            )
        )

        self.assertIs(received, response)

    def test_configured_checks_stop_after_first_failure(self) -> None:
        config = load_acceptance_actor_config(
            self.write_config(checks=["provider_ping", "reply_route"])
        )
        failed = SimpleNamespace(ok=False)

        with patch(
            "hermes_codex_router.acceptance_actor._run_check",
            new=AsyncMock(return_value=failed),
        ) as run_check:
            results = asyncio.run(_run_configured_checks(FakeClient(), config))

        self.assertEqual(results, [failed])
        run_check.assert_awaited_once_with(
            ANY,
            config,
            "provider_ping",
            "example_provider_bot",
        )

    def test_reply_route_targets_the_author_without_a_second_mention(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("reply_route",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        client = FakeClient()
        responses = (
            FakeMessage(10, "REPLY_PARENT_OK"),
            FakeMessage(12, "REPLY_CHILD_OK"),
        )
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(side_effect=responses),
        ):
            result = asyncio.run(_run_check(client, config, "reply_route", "example_provider_bot"))

        self.assertTrue(result.ok)
        self.assertEqual(client.sent[1][1]["reply_to"], 10)
        self.assertNotIn("@example_provider_bot", str(client.sent[1][0][1]))

    def test_burst_route_sends_one_instruction_as_three_immediate_messages(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("burst_route",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        client = FakeClient()
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(return_value=FakeMessage(10, "BURST_E2E_OK")),
        ) as wait:
            result = asyncio.run(_run_check(client, config, "burst_route", "example_provider_bot"))

        self.assertTrue(result.ok)
        self.assertEqual(len(client.sent), 3)
        self.assertIn("@example_provider_bot", str(client.sent[0][0][1]))
        self.assertNotIn("@example_provider_bot", str(client.sent[1][0][1]))
        call = wait.await_args
        assert call is not None
        self.assertEqual(call.kwargs["after_id"], 3)

    def test_stop_route_recovers_after_deterministic_emergency_stop(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("stop_route",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        client = FakeClient()
        responses = (
            FakeMessage(10, "Останавливаю активную работу."),
            FakeMessage(12, "AFTER_STOP_E2E_OK"),
        )
        with (
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(side_effect=responses),
            ),
            patch("hermes_codex_router.acceptance_actor.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(_run_check(client, config, "stop_route", "example_provider_bot"))

        self.assertTrue(result.ok)
        self.assertEqual(client.sent[1][0][1], "stop")
        self.assertIn("AFTER_STOP_E2E_OK", str(client.sent[2][0][1]))

    def test_stop_route_accepts_explicit_queued_job_cancellation(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("stop_route",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        client = FakeClient()
        responses = (
            FakeMessage(10, "Активной работы нет; отменено задач в очереди: 1."),
            FakeMessage(12, "AFTER_STOP_E2E_OK"),
        )
        with (
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(side_effect=responses),
            ),
            patch("hermes_codex_router.acceptance_actor.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(_run_check(client, config, "stop_route", "example_provider_bot"))

        self.assertTrue(result.ok)

    def test_forwarded_quote_is_passive_then_visible_as_context(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("forwarded_quote",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        client = FakeClient()
        source = FakeMessage(10, "FORWARD_SOURCE_OK")
        responses = (
            source,
            AcceptanceActorError("timed out as expected"),
            FakeMessage(13, "FORWARD_CONTEXT_OK"),
        )
        with (
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(side_effect=responses),
            ) as wait,
            patch(
                "hermes_codex_router.acceptance_actor._forward_to_topic",
                new=AsyncMock(return_value=11),
            ) as forward,
        ):
            result = asyncio.run(
                _run_check(client, config, "forwarded_quote", "example_provider_bot")
            )

        self.assertTrue(result.ok)
        forward.assert_awaited_once_with(client, config, source)
        self.assertEqual(wait.await_args_list[1].kwargs["timeout_seconds"], 5)
        self.assertIn("FORWARD_CONTEXT_OK", str(client.sent[-1][0][1]))

    def test_artifact_delivery_verifies_filename_and_exact_content(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot",),
            checks=("artifact_delivery",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
        )
        client = FakeClient()
        response = FakeDocumentMessage(10, "hub-artifact-e2e.md", b"HUB_ARTIFACT_E2E_OK\n")
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(return_value=response),
        ) as wait:
            result = asyncio.run(
                _run_check(client, config, "artifact_delivery", "example_provider_bot")
            )

        self.assertTrue(result.ok)
        call = wait.await_args
        assert call is not None
        self.assertTrue(call.kwargs["require_document"])

    def test_codex_interaction_v2_checks_four_observable_scenarios(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_codex_bot",),
            checks=("codex_interaction_v2",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
            provider_agent_ids=("codex",),
        )
        responses = (
            FakeMessage(10, "4."),
            FakeMessage(12, "Which audience should the fictional launch note address?"),
            FakeMessage(
                14,
                "Approach: compare the three fictional options. "
                "Recommendation: choose option B because it is reversible.",
            ),
            FakeDocumentMessage(16, "hub-contract-v2-e2e.md", b"HUB_CONTRACT_V2_E2E_OK\n"),
        )
        with (
            patch(
                "hermes_codex_router.acceptance_actor._select_provider",
                new=AsyncMock(),
            ) as select,
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(side_effect=responses),
            ) as wait,
        ):
            client = FakeClient()
            result = asyncio.run(
                _run_check(client, config, "codex_interaction_v2", "example_codex_bot")
            )

        self.assertTrue(result.ok)
        select.assert_awaited_once_with(client, config, "codex")
        self.assertEqual(len(client.sent), 4)
        self.assertIn("2 + 2", str(client.sent[0][0][1]))
        self.assertIn("underspecified", str(client.sent[1][0][1]))
        self.assertIn("three fictional options", str(client.sent[2][0][1]))
        self.assertIn("hub-contract-v2-e2e.md", str(client.sent[3][0][1]))
        self.assertTrue(wait.await_args_list[-1].kwargs["require_document"])

    def test_codex_interaction_v2_fails_when_ambiguity_is_not_clarified(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_codex_bot",),
            checks=("codex_interaction_v2",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
            provider_agent_ids=("codex",),
        )
        with (
            patch(
                "hermes_codex_router.acceptance_actor._select_provider",
                new=AsyncMock(),
            ),
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(side_effect=(FakeMessage(10, "4."), FakeMessage(12, "Done."))),
            ),
        ):
            result = asyncio.run(
                _run_check(FakeClient(), config, "codex_interaction_v2", "example_codex_bot")
            )

        self.assertFalse(result.ok)
        self.assertIn("clarification", result.detail)

    def test_context_contract_switches_without_handoff_then_requests_history(self) -> None:
        config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.secret,
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("source_provider_bot", "target_provider_bot"),
            checks=("context_contract",),
            timeout_seconds=15,
            artifacts_dir=self.artifacts,
            provider_agent_ids=("source", "target"),
        )
        source_provider = FakeButton(b"provider:source")
        source_model = FakeButton(b"choose:source:model")
        source_effort = FakeButton(b"use:source:model:high")
        provider = FakeButton(b"provider:target")
        model = FakeButton(b"choose:target:model")
        effort = FakeButton(b"use:target:model:high")
        responses = (
            FakeMessage(2, button=source_provider),
            FakeMessage(3, button=source_model),
            FakeMessage(4, button=source_effort),
            FakeMessage(5, "Source already active."),
            FakeMessage(7, "CONTEXT_SOURCE_E2E_7391\n\nSession footer"),
            FakeMessage(9, button=provider),
            FakeMessage(10, button=model),
            FakeMessage(11, button=effort),
            FakeMessage(12, "Target active. No prior agent history was injected."),
            FakeMessage(14, "CONTEXT_SWITCH_ISOLATED_OK\n\nSession footer"),
            FakeMessage(16, "I reviewed the selected Codex history."),
        )
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(side_effect=responses),
        ):
            result = asyncio.run(
                _run_check(FakeClient(), config, "context_contract", config.hub_username)
            )

        self.assertTrue(result.ok)
        self.assertTrue(
            all(
                button.clicked
                for button in (
                    source_provider,
                    source_model,
                    source_effort,
                    provider,
                    model,
                    effort,
                )
            )
        )

    def test_p0_p1_live_check_records_all_seven_scenarios(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        config = self.p0_config(state_path)
        client = FakeP0P1Client()
        probe = StatefulAcceptanceProbe()
        supervisor = StatefulServiceSupervisor()
        responses = (
            FakeMessage(201, "caption"),
            FakeMessage(202, "album"),
            FakeMessage(203, "active"),
            FakeMessage(204, "late"),
            FakeMessage(205, "recovery"),
            FakeMessage(206, "oversize"),
            FakeMessage(207, "Context remaining: 88.5%\nWeekly remaining: 42%"),
            FakeMessage(208, "Context remaining: 88.5%"),
            FakeMessage(209, "Codex\nexample account"),
        )
        context = P0P1ScenarioContext(client, config, probe, supervisor)
        with (
            patch("hermes_codex_router.p0_p1_acceptance._select_provider", new=AsyncMock()),
            patch(
                "hermes_codex_router.p0_p1_acceptance._wait_for_markers",
                new=AsyncMock(side_effect=responses),
            ) as wait,
        ):
            results = asyncio.run(run_p0_p1_live_scenario(context, "example_codex_bot"))

        self.assertEqual(
            [result.check for result in results],
            [
                "caption_only_document_provider_content",
                "album_provider_content",
                "attachment_during_active_turn_fifo",
                "restart_idempotency_and_recovery",
                "oversize_explicit_unavailable_notice",
                "p1_live_turn_context_and_quota_labels",
                "p1_status_context_and_accounts_read_only",
            ],
        )
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(wait.await_args_list[-2].kwargs["username"], config.hub_username)
        self.assertEqual(wait.await_args_list[-1].kwargs["username"], config.hub_username)
        self.assertTrue(
            any(len(payload) == 20 * 1024 * 1024 + 1 for payload in client.uploaded_payloads)
        )
        self.assertEqual(
            supervisor.actions,
            [
                "capture",
                "stop_worker",
                "check_worker",
                "restart_controller",
                "check_controller",
                "start_worker",
                "check_worker",
                "restore",
            ],
        )
        self.assertEqual(
            {101, 102, 103, 104, 105, 106},
            {message_id for kind, message_id in probe.calls if kind == "jobs"},
        )
        job_calls = [message_id for kind, message_id in probe.calls if kind == "jobs"]
        self.assertGreaterEqual(job_calls.count(105), 2)
        self.assertGreaterEqual(job_calls.count(106), 3)
        self.assertEqual(
            [call for call in probe.calls if call[0] == "materials"],
            [("materials", "album"), ("materials", "recovery")],
        )

    def test_p0_p1_live_does_not_start_a_preexisting_inactive_service(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        config = self.p0_config(state_path)
        supervisor = StatefulServiceSupervisor(worker_active=False)
        context = P0P1ScenarioContext(
            FakeP0P1Client(), config, StatefulAcceptanceProbe(), supervisor
        )
        results = asyncio.run(run_p0_p1_live_scenario(context, "example_codex_bot"))

        self.assertEqual(len(results), 1)
        self.assertEqual([result.check for result in results], [P0_P1_CHECKS[0]])
        self.assertFalse(results[0].ok)
        self.assertIn("requires the Controller and Codex worker", results[0].detail)
        self.assertEqual(supervisor.actions, ["capture", "restore"])

    def test_p0_p1_live_restores_worker_after_failure_following_stop(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        config = self.p0_config(state_path)
        probe = StatefulAcceptanceProbe(fail_message_id=106)
        supervisor = StatefulServiceSupervisor()
        responses = (
            FakeMessage(201, "caption"),
            FakeMessage(202, "album"),
            FakeMessage(203, "active"),
            FakeMessage(204, "late"),
        )
        context = P0P1ScenarioContext(FakeP0P1Client(), config, probe, supervisor)
        with (
            patch("hermes_codex_router.p0_p1_acceptance._select_provider", new=AsyncMock()),
            patch(
                "hermes_codex_router.p0_p1_acceptance._wait_for_markers",
                new=AsyncMock(side_effect=responses),
            ),
        ):
            results = asyncio.run(run_p0_p1_live_scenario(context, "example_codex_bot"))

        self.assertFalse(results[-1].ok)
        self.assertEqual([result.check for result in results], list(P0_P1_CHECKS[:4]))
        self.assertIn("named state probe failure", results[-1].detail)
        self.assertTrue(supervisor.worker_active)
        self.assertEqual(supervisor.actions[-1], "restore")

    def test_p0_p1_live_restores_services_when_controller_restart_fails(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        config = self.p0_config(state_path)
        supervisor = StatefulServiceSupervisor(fail_action="restart_controller")
        responses = (
            FakeMessage(201, "caption"),
            FakeMessage(202, "album"),
            FakeMessage(203, "active"),
            FakeMessage(204, "late"),
        )
        context = P0P1ScenarioContext(
            FakeP0P1Client(), config, StatefulAcceptanceProbe(), supervisor
        )
        with (
            patch("hermes_codex_router.p0_p1_acceptance._select_provider", new=AsyncMock()),
            patch(
                "hermes_codex_router.p0_p1_acceptance._wait_for_markers",
                new=AsyncMock(side_effect=responses),
            ),
        ):
            results = asyncio.run(run_p0_p1_live_scenario(context, "example_codex_bot"))

        self.assertFalse(results[-1].ok)
        self.assertEqual([result.check for result in results], list(P0_P1_CHECKS[:4]))
        self.assertIn("named Controller restart failure", results[-1].detail)
        self.assertTrue(supervisor.controller_active)
        self.assertTrue(supervisor.worker_active)
        self.assertEqual(supervisor.actions[-1], "restore")

    def test_p0_p1_live_preserves_original_failure_when_restoration_fails(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        config = self.p0_config(state_path)
        supervisor = StatefulServiceSupervisor(fail_restore=True)
        responses = (
            FakeMessage(201, "caption"),
            FakeMessage(202, "album"),
            FakeMessage(203, "active"),
            FakeMessage(204, "late"),
        )
        context = P0P1ScenarioContext(
            FakeP0P1Client(),
            config,
            StatefulAcceptanceProbe(fail_message_id=106),
            supervisor,
        )
        with (
            patch("hermes_codex_router.p0_p1_acceptance._select_provider", new=AsyncMock()),
            patch(
                "hermes_codex_router.p0_p1_acceptance._wait_for_markers",
                new=AsyncMock(side_effect=responses),
            ),
        ):
            results = asyncio.run(run_p0_p1_live_scenario(context, "example_codex_bot"))

        self.assertFalse(results[-1].ok)
        self.assertEqual([result.check for result in results], list(P0_P1_CHECKS[:4]))
        self.assertIn("named state probe failure", results[-1].detail)
        self.assertIn("service restoration failed: named restoration failure", results[-1].detail)

    def test_p0_p1_context_contains_only_the_four_reviewed_dependencies(self) -> None:
        self.assertEqual(
            [field.name for field in fields(P0P1ScenarioContext)],
            ["client", "config", "state_probe", "service_supervisor"],
        )

    def test_acceptance_actor_reexports_the_existing_contract_names(self) -> None:
        self.assertIs(AcceptanceActorConfig, ContractConfig)
        self.assertIs(AcceptanceActorError, ContractError)
        self.assertIs(AcceptanceCheckResult, ContractResult)

    def test_acceptance_actor_builds_the_explicit_p0_p1_context(self) -> None:
        state_path = self.base / "state.db"
        state_path.touch(mode=0o600)
        config = self.p0_config(state_path)
        client = FakeP0P1Client()
        probe = StatefulAcceptanceProbe()
        supervisor = StatefulServiceSupervisor()
        run_scenario = AsyncMock(return_value=[])

        with (
            patch(
                "hermes_codex_router.acceptance_actor.ReadOnlyAcceptanceState",
                return_value=probe,
            ),
            patch(
                "hermes_codex_router.acceptance_actor.FixedServiceSupervisor",
                return_value=supervisor,
            ),
            patch(
                "hermes_codex_router.acceptance_actor._run_p0_p1_live_scenario",
                new=run_scenario,
            ),
        ):
            results = asyncio.run(_run_p0_p1_live_checks(client, config, "example_codex_bot"))

        self.assertEqual(results, [])
        await_args = run_scenario.await_args
        if await_args is None:
            self.fail("scenario was not called")
        context, target = await_args.args
        self.assertIs(context.client, client)
        self.assertIs(context.config, config)
        self.assertIs(context.state_probe, probe)
        self.assertIs(context.service_supervisor, supervisor)
        self.assertEqual(target, "example_codex_bot")

    def test_raw_forward_targets_the_canary_forum_topic(self) -> None:
        config = load_acceptance_actor_config(self.write_config())
        client = FakeRawClient()

        message_id = asyncio.run(
            _forward_to_topic(client, config, FakeMessage(44, "FORWARD_SOURCE_OK"))
        )

        self.assertEqual(message_id, 91)
        self.assertIsNotNone(client.request)
        self.assertEqual(getattr(client.request, "top_msg_id"), 77)
        self.assertEqual(getattr(client.request, "id"), [44])


if __name__ == "__main__":
    unittest.main()
