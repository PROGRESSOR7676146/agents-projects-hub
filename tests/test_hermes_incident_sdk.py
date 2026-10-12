"""Optional real PTB/JobQueue witness: fake Bot, no network or installed service."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_codex_router.hermes_incident_plugin import CONFIG_ENV, install_incident_notices
from hermes_codex_router.incident_journal import IncidentJournal

AVAILABLE = (
    importlib.util.find_spec("telegram") is not None
    and importlib.util.find_spec("apscheduler") is not None
)
REQUIRED = os.getenv("HUB_REQUIRE_HERMES_NOTICE_SDK") == "1"


@unittest.skipUnless(AVAILABLE or REQUIRED, "optional PTB 22.8 + APScheduler fixture")
class RealNoticeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_error_label_is_not_proof_of_rejected_delivery(self) -> None:
        import telegram
        from telegram.ext import ExtBot
        from telegram.request import BaseRequest

        class OfflineRequest(BaseRequest):
            @property
            def read_timeout(self) -> float:
                return 1

            async def initialize(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
                # PTB still raises BadRequest despite this contradictory body.
                return 400, b'{"ok":true,"result":{"message_id":1}}'

        class OfflineBot(ExtBot):
            async def initialize(self) -> None:
                self._bot_user = telegram.User(987654321, "Example Bot", True)
                self._initialized = True

            async def shutdown(self) -> None:
                self._initialized = False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal, cursor, config = (
                root / "incident-journal.json",
                root / "hermes-incident-cursor.json",
                root / "notices.json",
            )
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "journal": str(journal),
                        "cursor": str(cursor),
                        "owner": 123456789,
                    }
                )
            )
            config.chmod(0o600)
            with IncidentJournal(journal).cycle() as writer:
                writer.project((), now=datetime.now(timezone.utc))
            request = OfflineRequest()
            do_request = mock.AsyncMock(wraps=request.do_request)
            with mock.patch.object(request, "do_request", do_request):
                await self._exercise_sdk_request(OfflineBot, request, config, cursor, "unknown")
            do_request.assert_awaited_once()

    async def test_real_sdk_success_records_positive_receipt(self) -> None:
        import telegram
        from telegram.ext import ExtBot
        from telegram.request import BaseRequest

        class OfflineRequest(BaseRequest):
            @property
            def read_timeout(self) -> float:
                return 1

            async def initialize(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
                return 200, json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "message_id": 41,
                            "date": 1767225600,
                            "chat": {"id": 123456789, "type": "private"},
                            "text": "Example summary",
                        },
                    }
                ).encode()

        class OfflineBot(ExtBot):
            async def initialize(self) -> None:
                self._bot_user = telegram.User(987654321, "Example Bot", True)
                self._initialized = True

            async def shutdown(self) -> None:
                self._initialized = False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal, cursor, config = (
                root / "journal.json",
                root / "cursor.json",
                root / "notices.json",
            )
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "journal": str(journal),
                        "cursor": str(cursor),
                        "owner": 123456789,
                    }
                )
            )
            config.chmod(0o600)
            with IncidentJournal(journal).cycle() as writer:
                writer.project((), now=datetime.now(timezone.utc))
            request = OfflineRequest()
            do_request = mock.AsyncMock(wraps=request.do_request)
            with mock.patch.object(request, "do_request", do_request):
                await self._exercise_sdk_request(OfflineBot, request, config, cursor, "sent")
            do_request.assert_awaited_once()
            self.assertTrue(do_request.call_args.kwargs["url"].endswith("/sendMessage"))
            self.assertEqual(do_request.call_args.kwargs["method"], "POST")
            parameters = do_request.call_args.kwargs["request_data"].json_parameters
            self.assertEqual(parameters["chat_id"], "123456789")
            self.assertEqual(json.loads(cursor.read_text())["deliveries"][0]["message_id"], 41)

    async def _exercise_sdk_request(
        self, bot_type: Any, request: Any, config: Path, cursor: Path, state: str
    ) -> None:
        from telegram.ext import Application

        bot = bot_type("123456789:EXAMPLE_NOT_A_REAL_TOKEN", request=request)
        app = Application.builder().bot(bot).updater(None).build()
        with mock.patch.dict(os.environ, {CONFIG_ENV: str(config)}):
            self.assertTrue(install_incident_notices(app))
            await app.initialize()
            try:
                integration = app.job_queue.jobs()[0].callback.__self__
                await integration.tick(None)
                self.assertEqual(json.loads(cursor.read_text())["deliveries"][0]["state"], state)
            finally:
                await app.shutdown()

    async def test_real_dispatch_stops_own_stale_controls_before_core(self) -> None:
        import telegram
        from telegram.ext import Application, CallbackQueryHandler, ExtBot

        class OfflineBot(ExtBot):
            async def initialize(self) -> None:
                self._bot_user = telegram.User(987654321, "Example Bot", True)
                self._initialized = True

            async def shutdown(self) -> None:
                self._initialized = False

            async def answer_callback_query(self, *args: Any, **kwargs: Any) -> bool:
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "notices.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "journal": str(root / "incident-journal.json"),
                        "cursor": str(root / "hermes-incident-cursor.json"),
                        "owner": 123456789,
                    }
                )
            )
            config.chmod(0o600)
            app = (
                Application.builder()
                .bot(OfflineBot("123456789:EXAMPLE_NOT_A_REAL_TOKEN"))
                .updater(None)
                .build()
            )
            core = mock.AsyncMock()
            with mock.patch.dict(os.environ, {CONFIG_ENV: str(config)}):
                self.assertTrue(install_incident_notices(app))
                app.add_handler(CallbackQueryHandler(core))
                await app.initialize()
                try:
                    for data in (
                        "hubinc:analyze:unknown",
                        "hubinc:hide:" + "a" * 32,
                        "hubinc:malformed",
                    ):
                        query = telegram.CallbackQuery(
                            "example-query",
                            telegram.User(123456789, "Example", False),
                            "example-chat-instance",
                            data=data,
                            message=telegram.Message(
                                1, datetime.now(timezone.utc), telegram.Chat(123456789, "private")
                            ),
                        )
                        query.set_bot(app.bot)
                        await app.process_update(telegram.Update(1, callback_query=query))
                    core.assert_not_awaited()
                finally:
                    await app.shutdown()
            self.assertFalse((root / "hermes-incident-cursor.json").exists())

    async def test_native_start_stop_join_and_reconnect_without_post_hooks(self) -> None:
        import telegram
        from telegram.ext import Application, ExtBot

        self.assertEqual(telegram.__version__, "22.8")
        sent: list[str] = []
        entered, release = asyncio.Event(), asyncio.Event()

        class OfflineBot(ExtBot):
            async def initialize(self) -> None:
                self._bot_user = telegram.User(987654321, "Example Bot", True)
                self._initialized = True

            async def shutdown(self) -> None:
                self._initialized = False

            async def send_message(self, **kwargs: Any) -> Any:
                sent.append(kwargs["text"])
                entered.set()
                await release.wait()
                return telegram.Message(
                    len(sent), datetime.now(timezone.utc), telegram.Chat(123456789, "private")
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "incident-journal.json"
            config = root / "notices.json"
            cursor = root / "hermes-incident-cursor.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "journal": str(journal),
                        "cursor": str(cursor),
                        "owner": 123456789,
                    }
                )
            )
            config.chmod(0o600)
            with IncidentJournal(journal).cycle() as writer:
                writer.project((), now=datetime.now(timezone.utc))
            with mock.patch.dict(os.environ, {CONFIG_ENV: str(config)}):
                for cycle in range(2):
                    post_init, post_shutdown = mock.AsyncMock(), mock.AsyncMock()
                    app = (
                        Application.builder()
                        .bot(OfflineBot("123456789:EXAMPLE_NOT_A_REAL_TOKEN"))
                        .updater(None)
                        .post_init(post_init)
                        .post_shutdown(post_shutdown)
                        .build()
                    )
                    self.assertTrue(install_incident_notices(app))
                    queue = app.job_queue
                    self.assertIsNotNone(queue)
                    assert queue is not None
                    job = queue.jobs()[0]
                    completed = asyncio.Event()
                    callback = job.callback

                    async def observed(context: Any) -> None:
                        try:
                            await callback(context)
                        finally:
                            completed.set()

                    job.callback = observed
                    await app.initialize()
                    try:
                        self.assertFalse(queue.scheduler.running)
                        await app.start()
                        self.assertTrue(queue.scheduler.running)
                        job.job.modify(next_run_time=datetime.now(timezone.utc))
                        if cycle == 0:
                            await asyncio.wait_for(entered.wait(), 5)
                            stopping = asyncio.create_task(app.stop())
                            await asyncio.sleep(0.03)
                            self.assertFalse(stopping.done(), "stop must join its active callback")
                            release.set()
                            await asyncio.wait_for(stopping, 5)
                        else:
                            # A restarted exact cursor sends no historical summary.
                            await asyncio.wait_for(completed.wait(), 5)
                            await app.stop()
                        self.assertFalse(queue.scheduler.running)
                    finally:
                        if app.running:
                            release.set()
                            await app.stop()
                        await app.shutdown()
                    post_init.assert_not_awaited()
                    post_shutdown.assert_not_awaited()
                self.assertEqual(len(sent), 1)
                self.assertEqual(json.loads(cursor.read_text())["deliveries"][0]["state"], "sent")


if __name__ == "__main__":
    unittest.main()
