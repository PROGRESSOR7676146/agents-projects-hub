from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hermes_codex_router.acceptance_actor import (
    AcceptanceActorConfig,
    AcceptanceActorError,
    _run_check,
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
    async def send_message(self, *_args: object, **_kwargs: object) -> FakeMessage:
        return FakeMessage(1)


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
            "checks": ["status", "accounts", "model_menu", "provider_ping"],
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
        self.assertEqual(config.checks, ("status", "accounts", "model_menu", "provider_ping"))
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


if __name__ == "__main__":
    unittest.main()
