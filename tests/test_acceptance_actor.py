from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from hermes_codex_router.acceptance_actor import (
    AcceptanceActorConfig,
    AcceptanceActorError,
    _forward_to_topic,
    _run_check,
    _run_configured_checks,
    _targets_for_check,
    _wait_for_response,
    load_acceptance_actor_config,
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


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send_message(self, *_args: object, **_kwargs: object) -> FakeMessage:
        self.sent.append((_args, _kwargs))
        return FakeMessage(len(self.sent))


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
