from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_codex_router.acceptance_actor import (
    AcceptanceActorConfig,
    AcceptanceActorError,
    _run_check,
    _run_configured_checks,
)
from tests.test_acceptance_actor import FakeClient, FakeIncomingMessage, FakeMessage


class ForwardClient(FakeClient):
    def __init__(self, passive_messages: list[FakeIncomingMessage]) -> None:
        super().__init__()
        self.passive_messages = passive_messages
        self.passive_polls = 0

    async def send_message(self, *args: object, **kwargs: object) -> FakeMessage:
        await super().send_message(*args, **kwargs)
        return FakeMessage(101 if len(self.sent) == 1 else 104)

    async def iter_messages(self, _chat_id: int, *, min_id: int, reverse: bool):
        assert reverse
        if min_id == 101:
            source = FakeIncomingMessage(102, "example_provider_bot")
            source.raw_text = "FORWARD_SOURCE_OK"
            yield source
        elif min_id == 103:
            self.passive_polls += 1
            for message in self.passive_messages:
                yield message
        elif min_id == 104:
            response = FakeIncomingMessage(105, "example_provider_bot")
            response.raw_text = "FORWARD_CONTEXT_OK"
            yield response
        else:
            raise AssertionError(f"unexpected observation boundary: {min_id}")


class ForwardSilenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=Path("/home/example/telegram-api-hash"),
            session_path=Path("/home/example/acceptance.session"),
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_provider_bot", "example_other_bot"),
            checks=("forwarded_quote",),
            timeout_seconds=15,
            artifacts_dir=Path("/home/example/acceptance"),
        )

    def run_forward(self, client: ForwardClient):
        async def scenario():
            elapsed = [0.0]

            async def advance(seconds: float) -> None:
                elapsed[0] += seconds

            with (
                patch(
                    "hermes_codex_router.acceptance_actor.asyncio.get_running_loop",
                    return_value=SimpleNamespace(time=lambda: elapsed[0]),
                ),
                patch("hermes_codex_router.acceptance_actor.asyncio.sleep", new=advance),
                patch(
                    "hermes_codex_router.acceptance_actor._forward_to_topic",
                    new=AsyncMock(return_value=103),
                ),
            ):
                return await _run_check(
                    client, self.config, "forwarded_quote", "example_provider_bot"
                )

        return asyncio.run(scenario())

    def test_unrelated_traffic_fails_without_sending_productive_follow_up(self) -> None:
        client = ForwardClient([FakeIncomingMessage(104, "unrelated_user", sender_id=42)])
        result = self.run_forward(client)
        self.assertFalse(result.ok)
        self.assertIn("unrelated traffic", result.detail)
        self.assertEqual(len(client.sent), 1)

    def test_any_configured_provider_response_fails_the_passive_window(self) -> None:
        for username in ("example_provider_bot", "EXAMPLE_OTHER_BOT"):
            with self.subTest(username=username):
                client = ForwardClient([FakeIncomingMessage(104, username)])
                result = self.run_forward(client)
                self.assertFalse(result.ok)
                self.assertEqual(result.response_message_id, 104)
                self.assertEqual(result.detail, "provider answered a passive forward")
                self.assertEqual(len(client.sent), 1)

    def test_clean_window_allows_only_then_sends_the_explicit_follow_up(self) -> None:
        client = ForwardClient(
            [
                FakeIncomingMessage(104, "acceptance_actor", sender_id=1),
                FakeIncomingMessage(105, "example_hub_bot"),
            ]
        )
        result = self.run_forward(client)
        self.assertTrue(result.ok)
        self.assertEqual(client.passive_polls, 10)
        self.assertEqual(len(client.sent), 2)

    def test_provider_answer_does_not_hide_later_contamination_in_the_batch(self) -> None:
        client = ForwardClient(
            [
                FakeIncomingMessage(104, "example_other_bot"),
                FakeIncomingMessage(105, "unrelated_user", sender_id=42),
            ]
        )
        result = self.run_forward(client)
        self.assertFalse(result.ok)
        self.assertIn("unrelated traffic", result.detail)
        self.assertEqual(len(client.sent), 1)

    def test_other_topic_traffic_does_not_contaminate_the_window(self) -> None:
        message = FakeIncomingMessage(104, "unrelated_user", sender_id=42)
        message.reply_to = SimpleNamespace(reply_to_top_id=88, reply_to_msg_id=88)
        client = ForwardClient([message])
        self.assertTrue(self.run_forward(client).ok)
        self.assertEqual(client.passive_polls, 10)

    def test_contamination_ends_the_run_before_the_next_provider(self) -> None:
        client = FakeClient()
        with (
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(
                    side_effect=(
                        FakeMessage(102, "FORWARD_SOURCE_OK"),
                        AcceptanceActorError("unrelated traffic"),
                    )
                ),
            ) as wait,
            patch(
                "hermes_codex_router.acceptance_actor._forward_to_topic",
                new=AsyncMock(return_value=103),
            ),
        ):
            results = asyncio.run(_run_configured_checks(client, self.config))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(wait.await_count, 2)

    def test_generic_observation_errors_are_never_interpreted_as_silence(self) -> None:
        for detail in ("history read failed", "timed out waiting for @example_provider_bot"):
            with self.subTest(detail=detail):
                client = FakeClient()
                responses = (
                    FakeMessage(102, "FORWARD_SOURCE_OK"),
                    AcceptanceActorError(detail),
                    FakeMessage(105, "FORWARD_CONTEXT_OK"),
                )
                with (
                    patch(
                        "hermes_codex_router.acceptance_actor._wait_for_response",
                        new=AsyncMock(side_effect=responses),
                    ),
                    patch(
                        "hermes_codex_router.acceptance_actor._forward_to_topic",
                        new=AsyncMock(return_value=103),
                    ),
                ):
                    result = asyncio.run(
                        _run_check(client, self.config, "forwarded_quote", "example_provider_bot")
                    )
                self.assertFalse(result.ok)
                self.assertEqual(result.detail, detail)
                self.assertEqual(len(client.sent), 1)


if __name__ == "__main__":
    unittest.main()
