"""Actual SQLite fences with fictional Telegram outcomes; no provider or network."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.artifacts import artifact_spool_root
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.telegram import TelegramBotApi, TelegramError
from tests import test_outbox_sender as fixtures


class ReceiptBot(fixtures.Bot):
    receipt: object = 71

    def send_html(self, *args: Any, **kwargs: Any) -> int:
        super().send_html(*args, **kwargs)
        return cast(int, self.receipt)

    def send_document(self, *args: Any, **kwargs: Any) -> int:
        super().send_document(*args, **kwargs)
        return cast(int, self.receipt)


class DeliveryCertaintyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def document_part(self, state: HubState, job_id: str):
        outbox = state.get_telegram_outbox_for_job(job_id)
        root = artifact_spool_root(self.fixture.config.state_path)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "example.md"
        data = b"Example artifact"
        path.write_bytes(data)
        path.chmod(0o600)
        with state._connection:
            state._connection.execute(
                """UPDATE telegram_outbox_parts SET part_type='document', file_path=?,
                   file_name='example.md', file_size=?, file_sha256=? WHERE outbox_id=?""",
                (str(path), len(data), hashlib.sha256(data).hexdigest(), outbox.outbox_id),
            )
        return path

    def test_unknown_document_retains_spool_and_cleanup_fault_never_resends(self) -> None:
        for index, unknown in ((91, True), (92, False)):
            with self.subTest(unknown=unknown):
                job_id = self.fixture.ready_outbox("opencode", index)
                bot = ReceiptBot()
                bot.receipt = None if unknown else 71
                sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
                try:
                    path = self.document_part(sender.state, job_id)
                    with patch(
                        "hermes_codex_router.final_delivery.remove_spooled_artifact",
                        side_effect=OSError("cleanup"),
                    ) as cleanup:
                        sender._deliver_one("opencode")
                        self.assertEqual(cleanup.call_count, 0 if unknown else 1)
                    outbox = sender.state.get_telegram_outbox_for_job(job_id)
                    self.assertEqual(outbox.status, "unknown" if unknown else "delivered")
                    self.assertTrue(path.exists())
                    self.assertFalse(sender._deliver_one("opencode"))
                    self.assertEqual(len(bot.documents), 1)
                finally:
                    sender.close()

    def test_embedded_cleanup_fault_preserves_committed_document_receipt(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 93)
        bot = ReceiptBot()
        service = cast(Any, object.__new__(ProjectHubService))
        service.config = self.fixture.config
        service.external_services = {"opencode": type("Identity", (), {"telegram": bot})()}
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            self.document_part(state, job_id)
            with patch(
                "hermes_codex_router.final_delivery.remove_spooled_artifact",
                side_effect=OSError("cleanup"),
            ):
                self.assertTrue(service._deliver_embedded_outbox(state, "opencode"))
            self.assertEqual(state.get_telegram_outbox_for_job(job_id).status, "delivered")
            self.assertFalse(service._deliver_embedded_outbox(state, "opencode"))
            self.assertEqual(len(bot.documents), 1)

    def test_clock_after_http_detects_expiry_and_unknown_parking_accepts_old_exact_token(
        self,
    ) -> None:
        from hermes_codex_router.final_delivery import deliver_final_part

        job_id = self.fixture.ready_outbox("opencode", 94)
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            outbox = state.lease_telegram_outbox("opencode", "example-sender", now=now)
            assert outbox is not None
            ticks = iter((now, now, now + timedelta(seconds=91), now + timedelta(seconds=92)))
            bot = ReceiptBot()
            result = deliver_final_part(
                state.delivery,
                bot,
                outbox,
                state_path=self.fixture.config.state_path,
                clock=lambda: next(ticks),
            )
            self.assertFalse(result.receipt_committed)
            self.assertEqual(state.get_telegram_outbox_for_job(job_id).status, "unknown")
            self.assertEqual(len(bot.sent), 1)

    def test_commit_then_raise_cannot_overwrite_committed_prefix(self) -> None:
        job_id = self.fixture.ready_outbox(
            "opencode", 95, telegram_html="".join(f"Example {n}\n" for n in range(1800))
        )
        bot = ReceiptBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        original = sender.state.delivery.mark_outbox_delivered

        def committed_then_raised(*args: Any, **kwargs: Any):
            original(*args, **kwargs)
            raise OSError("postcommit")

        try:
            with patch.object(
                sender.state.delivery, "mark_outbox_delivered", side_effect=committed_then_raised
            ):
                sender._deliver_one("opencode")
            outbox = sender.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(outbox.status, "pending")
            self.assertEqual(
                sender.state.get_telegram_outbox_parts(outbox.outbox_id)[0].telegram_message_id, 71
            )
            sender._deliver_one("opencode")
            self.assertEqual(len(bot.sent), 2)
            self.assertNotEqual(bot.sent[0][2], bot.sent[1][2])
        finally:
            sender.close()

    def test_malformed_success_parks_final_without_resend_or_completion(self) -> None:
        for index, receipt in enumerate((None, False, True, 0, -1, 1.5, "71"), start=1):
            with self.subTest(receipt=receipt):
                job_id = self.fixture.ready_outbox("opencode", index)
                bot = ReceiptBot()
                bot.receipt = receipt
                sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
                try:
                    self.assertTrue(sender._deliver_one("opencode"))
                    outbox = sender.state.get_telegram_outbox_for_job(job_id)
                    self.assertEqual(outbox.status, "unknown")
                    self.assertIsNotNone(outbox.send_started_at)
                    self.assertIsNone(outbox.telegram_message_id)
                    self.assertEqual(sender.state.get_provider_job(job_id).status, "result_ready")
                    self.assertIsNotNone(sender.state.get_provider_result(job_id))
                    self.assertFalse(sender._deliver_one("opencode"))
                    self.assertEqual(len(bot.sent), 1)
                finally:
                    sender.close()

    def test_embedded_uses_same_unknown_policy(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 20)
        bot = ReceiptBot()
        bot.receipt = None
        service = cast(Any, object.__new__(ProjectHubService))
        service.config = self.fixture.config
        service.external_services = {"opencode": type("Identity", (), {"telegram": bot})()}
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            self.assertTrue(service._deliver_embedded_outbox(state, "opencode"))
            self.assertEqual(state.get_telegram_outbox_for_job(job_id).status, "unknown")
            self.assertFalse(service._deliver_embedded_outbox(state, "opencode"))
        self.assertEqual(len(bot.sent), 1)

    def test_unknown_head_blocks_same_topic_delivery_and_execution_but_not_another_topic(
        self,
    ) -> None:
        head_id = self.fixture.ready_outbox("opencode", 21)
        later_id = self.fixture.ready_outbox("opencode", 22)
        independent_id = self.fixture.ready_outbox("opencode", 23)
        bot = ReceiptBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        try:
            state = sender.state
            head = state.get_provider_job(head_id)
            session = state.get_session(head.session_id)
            # Persisted fixtures also exercise defensive FIFO over already prepared results.
            with state._connection:
                state._connection.execute(
                    "UPDATE provider_jobs SET topic_id=?,topic_sequence=2 WHERE job_id=?",
                    (head.topic_id, later_id),
                )
                state._connection.execute(
                    "UPDATE topic_queue_counters SET next_sequence=3 WHERE topic_id=?",
                    (head.topic_id,),
                )
            tail, _ = state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:24",
                chat_id=-1001234567890,
                message_id=24,
                topic_id=head.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=session.provider_session_id,
                model=session.model,
                effort=session.effort,
                payload_text="Example queued successor",
                context_watermark=None,
                handoff_id=None,
            )
            bot.receipt = None
            self.assertTrue(sender._deliver_one("opencode"))
            self.assertEqual(state.get_telegram_outbox_for_job(head_id).status, "unknown")
            bot.receipt = 73
            self.assertTrue(sender._deliver_one("opencode"))
            self.assertEqual(state.get_telegram_outbox_for_job(independent_id).status, "delivered")
            self.assertFalse(sender._deliver_one("opencode"))
            self.assertIsNone(state.lease_provider_job("opencode", "example-worker"))
            self.assertEqual(state.get_telegram_outbox_for_job(later_id).status, "pending")
            self.assertEqual(state.get_provider_job(tail.job_id).status, "queued")
            self.assertEqual(state.get_provider_job(head_id).status, "result_ready")
            self.assertEqual(len(bot.sent), 2)
        finally:
            sender.close()

    def test_transport_error_is_unknown_unless_rejection_is_proven(self) -> None:
        cases = (
            (RuntimeError("fictional timeout"), "unknown"),
            (TelegramError("timeout", failure_class="network_timeout"), "unknown"),
            (TelegramError("server", failure_class="api_http", status_code=500), "unknown"),
            (TelegramError("timeout", failure_class="api_rejection", status_code=408), "unknown"),
            (TelegramError("rejected", failure_class="api_rejection", status_code=400), "pending"),
            (
                TelegramError("limited", failure_class="api_http", status_code=429, retry_after=60),
                "pending",
            ),
        )
        for index, (error, expected) in enumerate(cases, start=30):
            with self.subTest(error=error, expected=expected):
                # A prior pending retry can become due while the next case is
                # preparing. Independent queues keep the observed send bound
                # to this case even under slow parallel validation.
                fixture = fixtures.TelegramOutboxSenderTests()
                fixture.setUp()
                self.addCleanup(fixture.tearDown)
                job_id = fixture.ready_outbox("opencode", index)
                bot = fixtures.Bot(send_error=error)
                sender = fixture.sender(opencode=bot, antigravity=fixtures.Bot())
                try:
                    now = datetime.now(timezone.utc) + timedelta(seconds=1)
                    self.assertTrue(sender._deliver_one("opencode", now=now))
                    outbox = sender.state.get_telegram_outbox_for_job(job_id)
                    self.assertEqual(len(bot.sent), 1)
                    self.assertEqual(bot.sent[0][:2], (outbox.chat_id, outbox.thread_id))
                    self.assertEqual(outbox.status, expected)
                    if expected == "pending":
                        self.assertIsNone(outbox.send_started_at)
                        delay = (
                            60
                            if isinstance(error, TelegramError) and error.status_code == 429
                            else 1
                        )
                        self.assertEqual(
                            datetime.fromisoformat(outbox.available_at),
                            now + timedelta(seconds=delay),
                        )
                finally:
                    sender.close()

    def test_receipt_commit_exception_cannot_be_classified_as_transport_rejection(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 50)
        bot = ReceiptBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        try:
            with patch.object(
                sender.state.delivery,
                "mark_outbox_delivered",
                side_effect=TelegramError(
                    "commit fault", failure_class="api_rejection", status_code=400
                ),
            ):
                sender._deliver_one("opencode")
            self.assertEqual(sender.state.get_telegram_outbox_for_job(job_id).status, "unknown")
            self.assertFalse(sender._deliver_one("opencode"))
            self.assertEqual(len(bot.sent), 1)
        finally:
            sender.close()

    def test_restart_recovers_only_unattempted_lease(self) -> None:
        for index, attempted in ((61, False), (62, True)):
            job_id = self.fixture.ready_outbox("opencode", index)
            now = datetime.now(timezone.utc) + timedelta(seconds=1)
            with closing(
                HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
            ) as state:
                outbox = state.lease_telegram_outbox("opencode", "fictional-sender", now=now)
                assert outbox is not None and outbox.lease_token is not None
                part = state.next_telegram_outbox_part(
                    outbox.outbox_id, outbox.lease_token, now=now
                )
                if attempted:
                    state.delivery.begin_outbox_send(
                        outbox.outbox_id, outbox.lease_token, part.part_index, now=now
                    )
            with closing(
                HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
            ) as state:
                state.recover_stale_telegram_outbox(now=now + timedelta(seconds=91))
                outbox = state.get_telegram_outbox_for_job(job_id)
                self.assertEqual(outbox.status, "unknown" if attempted else "pending")
                self.assertEqual(state.get_provider_job(job_id).status, "result_ready")

    def test_multipart_prefix_survives_unknown_next_part(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 70, telegram_html="Example " * 1800)
        bot = ReceiptBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        try:
            sender._deliver_one("opencode")
            bot.receipt = None
            sender._deliver_one("opencode")
            outbox = sender.state.get_telegram_outbox_for_job(job_id)
            parts = sender.state.get_telegram_outbox_parts(outbox.outbox_id)
            self.assertEqual(outbox.status, "unknown")
            self.assertEqual(parts[0].telegram_message_id, 71)
            self.assertEqual(parts[0].receipt_validation_version, 1)
            self.assertIsNone(parts[1].telegram_message_id)
            self.assertFalse(sender._deliver_one("opencode"))
            self.assertEqual(len(bot.sent), 2)
        finally:
            sender.close()

    def test_receipt_requires_begun_fence_and_strict_id(self) -> None:
        self.fixture.ready_outbox("opencode", 80)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            outbox = state.lease_telegram_outbox("opencode", "fictional-sender")
            assert outbox is not None and outbox.lease_token is not None
            with self.assertRaises(StateError):
                state.mark_telegram_outbox_delivered(
                    outbox.outbox_id, outbox.lease_token, telegram_message_id=71
                )
            part = state.next_telegram_outbox_part(outbox.outbox_id, outbox.lease_token)
            state.delivery.begin_outbox_send(outbox.outbox_id, outbox.lease_token, part.part_index)
            for value in (True, False, 0, -1, 1.5, "71"):
                with self.subTest(value=value), self.assertRaises(StateError):
                    state.mark_telegram_outbox_delivered(
                        outbox.outbox_id, outbox.lease_token, telegram_message_id=cast(int, value)
                    )
            with self.assertRaises(StateError):
                state.release_telegram_outbox_lease(outbox.outbox_id, outbox.lease_token)

    def test_fence_failure_makes_no_transport_call_and_restart_is_conservative(self) -> None:
        from hermes_codex_router.final_delivery import deliver_final_part

        for index, committed in ((96, False), (97, True)):
            with self.subTest(committed=committed):
                job_id = self.fixture.ready_outbox("opencode", index)
                now = datetime.now(timezone.utc) + timedelta(seconds=1)
                bot = ReceiptBot()
                with closing(
                    HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
                ) as state:
                    outbox = state.lease_telegram_outbox("opencode", "example-sender", now=now)
                    assert outbox is not None
                    original = state.delivery.begin_outbox_send

                    def fault(*args: Any, **kwargs: Any):
                        if committed:
                            original(*args, **kwargs)
                        raise OSError("fence commit fault")

                    with patch.object(state.delivery, "begin_outbox_send", side_effect=fault):
                        result = deliver_final_part(
                            state.delivery,
                            bot,
                            outbox,
                            state_path=self.fixture.config.state_path,
                            now=now,
                        )
                    self.assertIsInstance(result.error, OSError)
                    self.assertFalse(result.receipt_committed)
                    self.assertEqual(bot.sent, [])
                with closing(
                    HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
                ) as state:
                    state.recover_stale_telegram_outbox(now=now + timedelta(seconds=91))
                    after = state.get_telegram_outbox_for_job(job_id)
                    self.assertEqual(after.status, "unknown" if committed else "pending")
                    self.assertEqual(after.attempt_count, 1 if committed else 0)
                    self.assertEqual(state.get_provider_job(job_id).status, "result_ready")
                    # Drain the unattempted fixture so the next case is not FIFO-blocked.
                    if not committed:
                        leased = state.lease_telegram_outbox(
                            "opencode", "example-sender", now=now + timedelta(seconds=91)
                        )
                        assert leased is not None
                        deliver_final_part(
                            state.delivery,
                            bot,
                            leased,
                            state_path=self.fixture.config.state_path,
                            now=now + timedelta(seconds=91),
                        )

    def test_wrong_part_and_stale_tokens_cannot_fence_or_park_a_new_lease(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 98, telegram_html="Example " * 1800)
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            first = state.lease_telegram_outbox("opencode", "example-old", now=now)
            assert first is not None and first.lease_token is not None
            part = state.next_telegram_outbox_part(first.outbox_id, first.lease_token, now=now)
            with self.assertRaises(StateError):
                state.delivery.begin_outbox_send(
                    first.outbox_id, first.lease_token, part.part_index + 1, now=now
                )
            self.assertIsNone(state.get_telegram_outbox_for_job(job_id).send_started_at)
            later = now + timedelta(seconds=91)
            state.recover_stale_telegram_outbox(now=later)
            second = state.lease_telegram_outbox("opencode", "example-new", now=later)
            assert second is not None and second.lease_token is not None
            self.assertNotEqual(first.lease_token, second.lease_token)
            with self.assertRaises(StateError):
                state.delivery.begin_outbox_send(
                    first.outbox_id, first.lease_token, part.part_index, now=later
                )
            state.delivery.begin_outbox_send(
                second.outbox_id, second.lease_token, part.part_index, now=later
            )
            before = state.get_telegram_outbox_for_job(job_id)
            self.assertFalse(
                state.delivery.mark_outbox_unknown(
                    first.outbox_id,
                    first.lease_token,
                    part.part_index,
                    error_code="example-late",
                    now=later,
                )
            )
            self.assertFalse(
                state.delivery.mark_outbox_unknown(
                    second.outbox_id,
                    second.lease_token,
                    part.part_index + 1,
                    error_code="example-wrong-part",
                    now=later,
                )
            )
            self.assertEqual(state.get_telegram_outbox_for_job(job_id), before)


class StrictTelegramReceiptTests(unittest.TestCase):
    def test_send_html_rejects_nonpositive_and_bool_receipts(self) -> None:
        api = TelegramBotApi("123456:example")
        for receipt in (None, False, True, 0, -1, 1.5, "71"):
            with (
                self.subTest(receipt=receipt),
                patch.object(api, "call", return_value={"message_id": receipt}),
            ):
                with self.assertRaises(TelegramError):
                    api.send_html(-1001234567890, 77, "Example")

    def test_send_document_rejects_malformed_receipts_without_retrying(self) -> None:
        api = TelegramBotApi("123456:example")
        with tempfile.TemporaryDirectory(prefix="example-receipt-") as root:
            path = Path(root) / "example.md"
            path.write_text("Example")
            for result in (
                None,
                [],
                {},
                *(dict(message_id=value) for value in (None, False, True, 0, -1, 1.5, "71")),
            ):
                with (
                    self.subTest(result=result),
                    patch.object(api, "_call_multipart", return_value=result) as transport,
                ):
                    with self.assertRaises(TelegramError) as caught:
                        api.send_document(-1001234567890, 77, path)
                    self.assertEqual(caught.exception.failure_class, "invalid_response")
                    transport.assert_called_once()
