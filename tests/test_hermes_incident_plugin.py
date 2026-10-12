from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_codex_router.hermes_incident_plugin import CONFIG_ENV, install_incident_notices
from hermes_codex_router.incident_journal import IncidentJournal

OWNER = 123456789


class Stop(Exception):
    pass


class Rejected(Exception):
    pass


class Retry(Rejected):
    retry_after = 5


class Job:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


class Queue:
    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.kwargs: dict[str, Any] = {}

    def run_repeating(self, callback: Any, **kwargs: Any) -> Job:
        self.kwargs = kwargs
        job = Job(callback)
        self.jobs.append(job)
        return job


class Handler:
    def __init__(self, callback: Any, pattern: str, block: bool = True) -> None:
        self.callback, self.pattern, self.block = callback, pattern, block


class Bot:
    id = 987654321

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.wrong_chat = False

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return types.SimpleNamespace(
            message_id=len(self.calls),
            chat=types.SimpleNamespace(id=OWNER + int(self.wrong_chat), type="private"),
        )


def sdk() -> dict[str, Any]:
    telegram = types.ModuleType("telegram")
    telegram.__dict__.update(
        __version__="22.8",
        InlineKeyboardButton=lambda text, callback_data: (text, callback_data),
        InlineKeyboardMarkup=lambda rows: rows,
    )
    ext = types.ModuleType("telegram.ext")
    ext.__dict__.update(ApplicationHandlerStop=Stop, CallbackQueryHandler=Handler)
    error = types.ModuleType("telegram.error")
    error.__dict__.update(BadRequest=Rejected, Forbidden=Rejected, RetryAfter=Retry)
    return {"telegram": telegram, "telegram.ext": ext, "telegram.error": error}


class HermesIncidentPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "notices.json"
        self.journal = self.root / "incident-journal.json"
        self.cursor = self.root / "hermes-incident-cursor.json"
        self.data = {
            "schema_version": 1,
            "journal": str(self.journal),
            "cursor": str(self.cursor),
            "owner": OWNER,
        }
        self.config.write_text(json.dumps(self.data))
        self.config.chmod(0o600)
        with IncidentJournal(self.journal).cycle() as writer:
            from datetime import datetime, timezone

            writer.project((), now=datetime.now(timezone.utc))
        env = mock.patch.dict("os.environ", {CONFIG_ENV: str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        modules = mock.patch.dict(sys.modules, sdk())
        modules.start()
        self.addCleanup(modules.stop)
        self.queue = Queue()
        self.handlers: list[tuple[Handler, int]] = []
        self.bot = Bot()
        self.application = types.SimpleNamespace(
            bot=self.bot,
            job_queue=self.queue,
            bot_data={},
            add_handler=lambda handler, group: self.handlers.append((handler, group)),
        )

    async def run_job(self) -> None:
        await self.queue.jobs[0].callback(types.SimpleNamespace(job=self.queue.jobs[0]))

    async def test_disabled_does_not_read_files_or_register_anything(self) -> None:
        with (
            mock.patch.dict("os.environ", {CONFIG_ENV: ""}),
            mock.patch(
                "hermes_codex_router.hermes_incident_plugin._load",
                side_effect=AssertionError("read"),
            ),
        ):
            self.assertFalse(install_incident_notices(self.application))
        self.assertEqual(self.queue.jobs, [])
        self.assertEqual(self.handlers, [])
        self.assertFalse(self.cursor.exists())

    async def test_native_jobqueue_owns_periodic_work_and_registration_is_unique(self) -> None:
        self.assertTrue(install_incident_notices(self.application))
        self.assertTrue(install_incident_notices(self.application))
        self.assertEqual(len(self.queue.jobs), 1)
        self.assertEqual(self.queue.kwargs["job_kwargs"], {"max_instances": 1, "coalesce": True})
        self.assertEqual(self.handlers[0][1], -30)
        await self.run_job()
        self.assertEqual(len(self.bot.calls), 1)
        call = self.bot.calls[0]
        self.assertEqual(call["chat_id"], OWNER)
        self.assertIsNone(call["parse_mode"])
        self.assertEqual(len(call["reply_markup"][0]), 1)
        self.assertEqual(call["reply_markup"][0][0][0], "Скрыть")

    async def test_missing_queue_or_wrong_sdk_refuses_opt_in(self) -> None:
        self.application.job_queue = None
        self.assertFalse(install_incident_notices(self.application))
        self.application.job_queue = self.queue
        with mock.patch.object(sys.modules["telegram"], "__version__", "99.0"):
            self.assertFalse(install_incident_notices(self.application))
        self.assertEqual(self.handlers, [])
        self.assertFalse(self.cursor.exists())

    async def test_unsafe_or_ambiguous_configuration_is_preserved(self) -> None:
        for content in (
            json.dumps({**self.data, "token": "forbidden"}),
            json.dumps({**self.data, "owner": True}),
            '{"schema_version":1,"schema_version":1}',
        ):
            self.config.write_text(content)
            self.assertFalse(install_incident_notices(self.application))
        self.config.write_text(json.dumps(self.data))
        self.config.chmod(0o644)
        self.assertFalse(install_incident_notices(self.application))
        self.assertEqual(self.bot.calls, [])

    async def test_config_change_retires_job_without_retargeting(self) -> None:
        install_incident_notices(self.application)
        self.config.write_text(json.dumps({**self.data, "owner": OWNER + 1}))
        await self.run_job()
        self.assertTrue(self.queue.jobs[0].removed)
        self.assertEqual(self.bot.calls, [])
        self.assertFalse(self.cursor.exists())

    async def test_only_explicit_sdk_rejection_is_retryable(self) -> None:
        install_incident_notices(self.application)
        self.bot.error = Retry("private details")
        await self.run_job()
        saved = json.loads(self.cursor.read_text())
        self.assertEqual(saved["deliveries"][0]["state"], "pending")
        self.assertNotIn("private details", self.cursor.read_text())
        self.cursor.unlink()
        self.bot.error = OSError("private details")
        await self.run_job()
        self.assertEqual(json.loads(self.cursor.read_text())["deliveries"][0]["state"], "unknown")
        await self.run_job()
        self.assertEqual(len(self.bot.calls), 2)

    async def test_wrong_destination_receipt_remains_unknown(self) -> None:
        install_incident_notices(self.application)
        self.bot.wrong_chat = True
        await self.run_job()
        self.assertEqual(json.loads(self.cursor.read_text())["deliveries"][0]["state"], "unknown")

    async def test_own_callback_namespace_always_stops_core_without_model_dispatch(self) -> None:
        install_incident_notices(self.application)
        answer = mock.AsyncMock()
        query = types.SimpleNamespace(
            data="hubinc:analyze:unknown",
            from_user=types.SimpleNamespace(id=OWNER, is_bot=False),
            message=types.SimpleNamespace(
                message_id=1, chat=types.SimpleNamespace(id=OWNER, type="private")
            ),
            answer=answer,
        )
        with self.assertRaises(Stop):
            await self.handlers[0][0].callback(types.SimpleNamespace(callback_query=query), None)
        answer.assert_awaited_once()
        self.assertEqual(self.bot.calls, [])

    async def test_control_rechecks_config_and_owner_before_dismiss(self) -> None:
        install_incident_notices(self.application)
        await self.run_job()
        integration = self.application.bot_data["hub_incident_notices_v1"]
        dismiss = mock.Mock(return_value=True)
        integration.consumer.dismiss = dismiss
        token = json.loads(self.cursor.read_text())["deliveries"][0]["token"]
        query = types.SimpleNamespace(
            data=f"hubinc:hide:{token}",
            from_user=types.SimpleNamespace(id=OWNER, is_bot=True),
            message=types.SimpleNamespace(
                message_id=1, chat=types.SimpleNamespace(id=OWNER, type="private")
            ),
            answer=mock.AsyncMock(),
        )
        with self.assertRaises(Stop):
            await self.handlers[0][0].callback(types.SimpleNamespace(callback_query=query), None)
        dismiss.assert_not_called()
        query.from_user.is_bot = False
        with self.assertRaises(Stop):
            await self.handlers[0][0].callback(types.SimpleNamespace(callback_query=query), None)
        dismiss.assert_called_once_with(token, OWNER, OWNER, 1)
        dismiss.reset_mock()
        self.config.write_text(json.dumps({**self.data, "owner": OWNER + 1}))
        with self.assertRaises(Stop):
            await self.handlers[0][0].callback(types.SimpleNamespace(callback_query=query), None)
        dismiss.assert_not_called()

    async def test_failed_handler_registration_retires_its_owned_job(self) -> None:
        self.application.add_handler = mock.Mock(side_effect=OSError("private"))
        self.assertFalse(install_incident_notices(self.application))
        self.assertTrue(self.queue.jobs[0].removed)
        self.assertEqual(self.bot.calls, [])


if __name__ == "__main__":
    unittest.main()
