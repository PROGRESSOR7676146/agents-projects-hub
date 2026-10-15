"""Native pre-connect failures retain durable fences, prefixes and cooldowns."""

from __future__ import annotations

import os
import socket
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.final_delivery import deliver_final_part
from hermes_codex_router.progress_delivery import ProgressDeliveryQueue
from hermes_codex_router.progress_delivery_sender import deliver_progress
from hermes_codex_router.state import HubState
from hermes_codex_router.task_notice_sender import deliver_task_notice
from hermes_codex_router.telegram import TelegramBotApi
from tests import test_delivery_certainty as final_fixtures
from tests import test_progress_delivery as progress_fixtures
from tests import test_task_lifecycle as notice_fixtures
from tests.test_telegram_not_sent import response_socket


class NotSentDeliveryRetryTests(unittest.TestCase):
    def test_final_multipart_keeps_prefix_and_retry_deadline_across_restart(self) -> None:
        fixture = final_fixtures.DeliveryCertaintyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state_path = fixture.fixture.config.state_path
        job_id = fixture.fixture.ready_outbox("opencode", 141, telegram_html="Example " * 1800)
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        bot = TelegramBotApi("123456:example")
        with closing(HubState.open(state_path, codex_permission_profile=None)) as state:
            before = state.get_provider_job(job_id)
            first = state.lease_telegram_outbox("opencode", "example-sender", now=now)
            assert first is not None
            sock = response_socket()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("socket.create_connection", return_value=sock),
                patch("ssl.SSLContext.wrap_socket", return_value=sock),
            ):
                self.assertTrue(
                    deliver_final_part(
                        state.delivery, bot, first, state_path=state_path, now=now
                    ).receipt_committed
                )
            second = state.lease_telegram_outbox("opencode", "example-sender", now=now)
            assert second is not None

            def failed_connect(*_args, **_kwargs):
                fenced = state.get_telegram_outbox_for_job(job_id)
                self.assertEqual(fenced.status, "sending")
                self.assertIsNotNone(fenced.send_started_at)
                raise socket.gaierror(socket.EAI_AGAIN, "fictional DNS failure")

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("socket.create_connection", side_effect=failed_connect) as connect,
            ):
                result = deliver_final_part(
                    state.delivery, bot, second, state_path=state_path, now=now
                )
            connect.assert_called_once()
            self.assertIsNotNone(result.error)
            pending = state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(pending.status, "pending")
            self.assertIsNone(pending.send_started_at)
            self.assertEqual(state.get_provider_job(job_id), before)
            parts = state.get_telegram_outbox_parts(first.outbox_id)
            self.assertEqual(parts[0].telegram_message_id, 71)
            self.assertEqual(parts[0].receipt_validation_version, 1)
            self.assertIsNone(parts[1].telegram_message_id)
        with closing(HubState.open(state_path, codex_permission_profile=None)) as state:
            self.assertIsNone(state.lease_telegram_outbox("opencode", "new-sender", now=now))
            self.assertEqual(state.get_telegram_outbox_for_job(job_id), pending)
            later = datetime.fromisoformat(pending.available_at) + timedelta(seconds=1)
            next_part = state.lease_telegram_outbox("opencode", "new-sender", now=later)
            assert next_part is not None
            sock = response_socket(b'{"ok":true,"result":{"message_id":72}}')
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("socket.create_connection", return_value=sock),
                patch("ssl.SSLContext.wrap_socket", return_value=sock),
            ):
                deliver_final_part(state.delivery, bot, next_part, state_path=state_path, now=later)
            parts = state.get_telegram_outbox_parts(first.outbox_id)
            self.assertEqual([part.telegram_message_id for part in parts[:2]], [71, 72])
            self.assertEqual(state.get_provider_job(job_id), before)

    def test_document_dns_failure_keeps_spool_without_unknown_hold(self) -> None:
        fixture = final_fixtures.DeliveryCertaintyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state_path = fixture.fixture.config.state_path
        job_id = fixture.fixture.ready_outbox("opencode", 142)
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with closing(HubState.open(state_path, codex_permission_profile=None)) as state:
            path = fixture.document_part(state, job_id)
            outbox = state.lease_telegram_outbox("opencode", "example-sender", now=now)
            assert outbox is not None
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("socket.create_connection", side_effect=socket.gaierror("DNS")),
            ):
                deliver_final_part(
                    state.delivery,
                    TelegramBotApi("123456:example"),
                    outbox,
                    state_path=state_path,
                    now=now,
                )
            self.assertEqual(state.get_telegram_outbox_for_job(job_id).status, "pending")
            self.assertTrue(path.exists())
            self.assertEqual(state.get_provider_job(job_id).status, "result_ready")

    def test_progress_connection_failure_retries_without_execution_change(self) -> None:
        fixture = progress_fixtures.DurableProgressDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job_id, token, journal = fixture.executing_job()
        journal.record_item(job_id, token, "example-item", "Example progress", "commentary")
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        before = fixture.state.get_provider_job(job_id)
        progress = fixture.state.delivery.lease_progress("codex", "example-sender", now=now)
        assert progress is not None
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")),
        ):
            result = deliver_progress(
                fixture.state.delivery, TelegramBotApi("123456:example"), progress, now=now
            )
        self.assertIsNotNone(result.error)
        pending = ProgressDeliveryQueue(fixture.state).for_job(job_id)[0]
        self.assertEqual(pending.status, "pending")
        self.assertIsNone(pending.send_started_at)
        self.assertIsNone(fixture.state.delivery.lease_progress("codex", "example-sender", now=now))
        self.assertEqual(fixture.state.get_provider_job(job_id), before)

    def test_control_notice_retries_without_changing_job(self) -> None:
        fixture = notice_fixtures.TaskLifecycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        notice, _ = fixture.prepare()
        before = fixture.db.execute("SELECT * FROM provider_jobs").fetchall()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("socket.create_connection", side_effect=socket.gaierror("DNS")),
        ):
            deliver_task_notice(
                fixture.state, TelegramBotApi("123456:example"), "example-sender", now=fixture.now
            )
        pending = fixture.state.get_notice(notice.notice_id)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.available_at, (fixture.now + timedelta(seconds=1)).isoformat())
        self.assertEqual(fixture.db.execute("SELECT * FROM provider_jobs").fetchall(), before)
