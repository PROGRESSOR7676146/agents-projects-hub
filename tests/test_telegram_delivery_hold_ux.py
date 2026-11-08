"""Offline Hub-Bot recovery of one unknown final-result delivery hold."""

from __future__ import annotations

import html
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router import artifact_capabilities
from hermes_codex_router.hub_config import AcceptanceActor, HubTelegramBot
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.task_notice_sender import deliver_task_notice
from hermes_codex_router.telegram import TelegramError
from hermes_codex_router.telegram_delivery_hold_state import CALLBACK_ORIGIN, CALLBACK_PREFIX
from tests.hub_service_harness import ANTIGRAVITY, CHAT_ID, CODEX, OWNER_ID, THREAD_ID, HubHarness


def _topic_update(
    message_id: int,
    text: str,
    *,
    sender_id: int = OWNER_ID,
    chat_id: int = CHAT_ID,
    thread_id: int = THREAD_ID,
    reply_to_message_id: int | None = None,
    reply_to_username: str = "example_hub_bot",
    reply_to_sender_id: int = 700,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": message_id,
        "message_thread_id": thread_id,
        "is_topic_message": True,
        "from": {"id": sender_id, "is_bot": False},
        "chat": {"id": chat_id, "type": "supergroup", "title": "Example"},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "from": {
                "id": reply_to_sender_id,
                "is_bot": True,
                "username": reply_to_username,
            },
        }
    return {"update_id": message_id, "message": message}


def _callback_update(
    message_id: int,
    data: str,
    *,
    callback_id: str = "callback-1",
    sender_id: int = OWNER_ID,
    chat_id: int = CHAT_ID,
    thread_id: int = THREAD_ID,
    message_username: str = "example_hub_bot",
    message_sender_id: int = 700,
) -> dict[str, object]:
    return {
        "update_id": 10_000 + message_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": sender_id, "is_bot": False},
            "data": data,
            "message": {
                "message_id": message_id,
                "message_thread_id": thread_id,
                "from": {
                    "id": message_sender_id,
                    "is_bot": True,
                    "username": message_username,
                },
                "chat": {"id": chat_id, "type": "supergroup", "title": "Example"},
            },
        },
    }


class TelegramDeliveryHoldUXTests(unittest.TestCase):
    def setUp(self) -> None:
        capability = patch.object(artifact_capabilities, "REMOTE_DELIVERY_HOLD_RECOVERY", True)
        capability.start()
        self.addCleanup(capability.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.hub = HubHarness(Path(self.temporary.name))
        self.addCleanup(self.hub.close)
        self.addCleanup(self.temporary.cleanup)
        self.hub.with_config(
            hub_bot=HubTelegramBot(
                "example_hub_bot", self.hub.root.parent / "hub-token", telegram_user_id=700
            ),
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=(CODEX.agent_id,),
        )
        self.hub.service.ingress_identity = "hub"
        self.state = self.hub.service.state
        self.topic = self.hub.topic()
        self.session = self.hub.activate(CODEX)
        self.now = datetime.now(timezone.utc)

        job, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:41",
            chat_id=CHAT_ID,
            message_id=41,
            topic_id=self.topic.topic_id,
            agent_id=CODEX.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=self.session.provider_session_id,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fictional saved request",
            context_watermark=None,
            handoff_id=None,
        )
        lease = self.state.lease_provider_job(CODEX.agent_id, "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(lease.job_id, lease.lease_token)
        self.state.commit_provider_result(
            job.job_id,
            lease.lease_token,
            visible_response="Fictional saved result",
            telegram_html="Fictional saved result",
            sender_agent_id=CODEX.agent_id,
        )
        self.outbox = self.state.get_telegram_outbox_for_job(job.job_id)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox SET status='unknown',error_code='fictional_timeout' "
                "WHERE outbox_id=?",
                (self.outbox.outbox_id,),
            )
        self.assertEqual(
            self.state.delivery.prepare_delivery_wait_notices(
                self.state.task_notices, sender_agent_ids=(CODEX.agent_id,), now=self.now
            ),
            1,
        )
        warning_bot = Mock()
        warning_bot.send_html.return_value = 901
        self.assertTrue(
            deliver_task_notice(
                self.state.task_notices, warning_bot, "example-sender", now=self.now
            ).delivered
        )
        self.hub.telegram.sent.clear()
        self.hub.telegram.markups.clear()

    def _protected_evidence(self) -> dict[str, list[tuple[object, ...]]]:
        tables = (
            "provider_jobs",
            "provider_job_results",
            "telegram_outbox",
            "telegram_outbox_parts",
            "telegram_send_attempts",
            "telegram_send_receipts",
            "task_lifecycle_notices",
            "agent_sessions",
        )
        return {
            table: [tuple(row) for row in self.state._connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }

    def _preview(self, *, message_id: int = 42) -> str:
        self.assertTrue(
            self.hub.service.handle_update(
                _topic_update(message_id, "/delivery", reply_to_message_id=901)
            )
        )
        self.assertEqual(len(self.hub.telegram.sent), 1)
        markup = self.hub.telegram.markups[-1]
        assert isinstance(markup, dict)
        callback = markup["inline_keyboard"][0][0]["callback_data"]
        assert isinstance(callback, str)
        self.assertTrue(callback.startswith(CALLBACK_PREFIX))
        return callback

    def _apply(
        self,
        callback: str,
        *,
        callback_id: str = "callback-1",
        state: HubState | None = None,
        now: datetime | None = None,
    ):
        target = state or self.state
        return target.apply_telegram_delivery_hold_callback(
            callback.removeprefix(CALLBACK_PREFIX),
            owner_user_id=OWNER_ID,
            owner_user_ids=(OWNER_ID,),
            chat_id=CHAT_ID,
            thread_id=THREAD_ID,
            callback_message_id=1,
            hub_bot_user_id=700,
            callback_id=callback_id,
            callback_origin=CALLBACK_ORIGIN,
            now=now or datetime.now(timezone.utc),
        )

    def _enqueue_tail(self, message_id: int = 43):
        job = self.state.get_provider_job(self.outbox.job_id)
        session = self.state.get_session(job.session_id)
        tail, _ = self.state.enqueue_provider_job(
            idempotency_key=f"telegram:{CHAT_ID}:{message_id}",
            chat_id=CHAT_ID,
            message_id=message_id,
            topic_id=job.topic_id,
            agent_id=CODEX.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=session.provider_session_id,
            model=session.model,
            effort=session.effort,
            payload_text="Fictional queued successor",
            context_watermark=None,
            handoff_id=None,
        )
        return tail

    def _copy_callback(self) -> str:
        self._preview()
        markup = self.hub.telegram.markups[-1]
        assert isinstance(markup, dict)
        self.assertEqual(
            [row[0]["text"] for row in markup["inline_keyboard"]],
            ["Continue without confirmed delivery", "Send me a copy of the saved answer"],
        )
        return markup["inline_keyboard"][1][0]["callback_data"]

    def test_private_copy_preserves_evidence_and_fifo(self) -> None:
        self._enqueue_tail()
        callback = self._copy_callback()
        before = self._protected_evidence()
        with patch.object(self.hub.telegram, "send_html", return_value=22) as send:
            self.assertTrue(self.hub.service.handle_update(_callback_update(1, callback)))
        self.assertEqual(send.call_args.args[:2], (OWNER_ID, 1))
        text = send.call_args.args[2]
        self.assertIn("Fictional saved result", text)
        self.assertIn(html.escape(self.topic.title), text)
        self.assertIn("копи", text.lower())
        self.assertIn("не подтвержден", text.lower())
        self.assertEqual(self._protected_evidence(), before)
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))
        self.assertEqual(self.hub.client.turns, 0)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_long_private_copy_is_one_private_document_and_deleted(self) -> None:
        result = "<Fictional & result>\n" * 200
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_job_results SET visible_response=? WHERE job_id=?",
                (result, self.outbox.job_id),
            )
        callback = self._copy_callback()
        captured = []

        def send_document(chat: int, thread: int, path: Path, **kwargs: object) -> int:
            self.assertEqual((chat, thread), (OWNER_ID, 1))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            captured.append((path, path.read_text(encoding="utf-8"), kwargs))
            return 22

        with (
            patch.object(
                self.hub.telegram, "send_document", create=True, side_effect=send_document
            ),
            patch.object(self.hub.telegram, "send_html") as send,
        ):
            self.hub.service.handle_update(_callback_update(1, callback))
        send.assert_not_called()
        self.assertEqual(len(captured), 1)
        path, content, kwargs = captured[0]
        self.assertEqual(content, result)
        self.assertIn("копи", str(kwargs["caption"]).lower())
        self.assertFalse(path.exists())

    def test_copy_rejects_foreign_and_stale_callbacks(self) -> None:
        callback = self._copy_callback()
        with patch.object(self.hub.telegram, "send_html") as send:
            for update in (
                _callback_update(1, callback, sender_id=900),
                _callback_update(1, callback, chat_id=CHAT_ID - 1),
                _callback_update(1, callback, thread_id=THREAD_ID + 1),
                _callback_update(2, callback),
                _callback_update(1, callback, message_sender_id=701),
                _callback_update(1, callback + "x"),
            ):
                self.hub.service.handle_update(update)
            with self.state._connection:
                self.state._connection.execute(
                    "UPDATE provider_job_results SET visible_response='changed' WHERE job_id=?",
                    (self.outbox.job_id,),
                )
            self.hub.service.handle_update(_callback_update(1, callback))
        send.assert_not_called()

    def test_expired_copy_is_refused(self) -> None:
        callback = self._copy_callback()
        issued = datetime.now(timezone.utc) - timedelta(minutes=11)
        with self.state._connection:
            self.state._connection.execute(
                "DROP TRIGGER telegram_delivery_hold_callback_tokens_no_update"
            )
            self.state._connection.execute(
                "UPDATE telegram_delivery_hold_callback_tokens SET issued_at=?,expires_at=?",
                (issued.isoformat(), (issued + timedelta(minutes=10)).isoformat()),
            )
        with patch.object(self.hub.telegram, "send_html") as send:
            self.hub.service.handle_update(_callback_update(1, callback))
        send.assert_not_called()

    def test_copy_escapes_text_and_discloses_omitted_attachments(self) -> None:
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_job_results SET visible_response=? WHERE job_id=?",
                ("<saved & answer>", self.outbox.job_id),
            )
            self.state._connection.execute(
                "INSERT INTO telegram_outbox_parts "
                "(outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,file_sha256) "
                "VALUES (?,2,'attachment','document','/home/example/file','file',1,?)",
                (self.outbox.outbox_id, "a" * 64),
            )
        callback = self._copy_callback()
        with patch.object(self.hub.telegram, "send_html", return_value=22) as send:
            self.hub.service.handle_update(_callback_update(1, callback))
            self.hub.service.handle_update(_callback_update(1, callback, callback_id="repeat"))
        self.assertEqual(send.call_count, 2)
        text = send.call_args.args[2]
        self.assertTrue(text.endswith("&lt;saved &amp; answer&gt;"))
        self.assertIn("Вложения оригинала не включены", text)

    def test_private_chat_unavailable_gives_content_free_notice(self) -> None:
        callback = self._copy_callback()
        with (
            patch.object(
                self.hub.telegram,
                "send_html",
                side_effect=[
                    TelegramError("forbidden", failure_class="api_rejection", status_code=403),
                    23,
                ],
            ) as send,
            patch.object(self.hub.telegram, "answer_callback") as answer,
        ):
            self.hub.service.handle_update(_callback_update(1, callback))
        self.assertEqual(send.call_args_list[1].args[:2], (CHAT_ID, THREAD_ID))
        self.assertNotIn("Fictional saved result", send.call_args_list[1].args[2])
        self.assertIn("Start", send.call_args_list[1].args[2])
        answer.assert_called_once()

    def test_ambiguous_private_copy_is_not_retried(self) -> None:
        callback = self._copy_callback()
        with (
            patch.object(
                self.hub.telegram, "send_html", side_effect=TelegramError("timeout")
            ) as send,
            patch.object(self.hub.telegram, "answer_callback") as answer,
        ):
            self.hub.service.handle_update(_callback_update(1, callback))
        send.assert_called_once()
        self.assertIn("ещё раз", answer.call_args.args[1])

    def test_hub_owner_can_preview_then_explicitly_continue_one_exact_hold(self) -> None:
        """A delivered Hub warning can create a bounded preview and one safe decision."""
        tail = self._enqueue_tail()
        before = self._protected_evidence()
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))
        with patch.object(
            self.hub.telegram, "send_html", wraps=self.hub.telegram.send_html
        ) as send:
            callback = self._preview()
        self.assertEqual(send.call_args.kwargs["reply_to_message_id"], 901)
        self.assertLessEqual(len(callback.encode("utf-8")), 64)
        self.assertNotIn(callback.removeprefix(CALLBACK_PREFIX), self.hub.telegram.sent[-1])
        self.assertNotIn(self.outbox.outbox_id, self.hub.telegram.sent[-1])
        self.assertNotIn("root:", self.hub.telegram.sent[-1])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            1,
        )

        self.assertTrue(self.hub.service.handle_update(_callback_update(1, callback)))
        self.assertEqual(self._protected_evidence(), before)
        self.assertEqual(self.state.get_telegram_outbox(self.outbox.outbox_id).status, "unknown")
        self.assertEqual(self.state.get_provider_job(self.outbox.job_id).status, "result_ready")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.state.preview_delivery_hold(self.outbox.outbox_id).hold_status,
            "released_by_telegram_owner",
        )
        lease = self.state.lease_provider_job(CODEX.agent_id, "example-worker")
        assert lease is not None
        self.assertEqual(lease.job_id, tail.job_id)
        self.assertEqual(len(self.hub.telegram.sent), 1)

    def test_hub_decision_does_not_bypass_native_writer_or_stop_guards(self) -> None:
        callback = self._preview()
        self._apply(callback)
        job = self.state.get_provider_job(self.outbox.job_id)
        with self.assertRaises(StateError):
            self.state.new_active_session(job.topic_id, expected_session_id=job.session_id)
        with self.assertRaises(StateError):
            self.state.set_writer_mode(job.session_id, "local")

        tail = self._enqueue_tail()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='indeterminate' WHERE job_id=?",
                (job.job_id,),
            )
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='result_ready' WHERE job_id=?",
                (job.job_id,),
            )
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (tail.session_id,),
            )
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='telegram' WHERE session_id=?",
                (tail.session_id,),
            )
            self.state._connection.execute(
                "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at) VALUES(?,?,?)",
                (tail.job_id, job.job_id, datetime.now(timezone.utc).isoformat()),
            )
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))
        self.state.request_emergency_stop(
            topic_id=tail.topic_id,
            chat_id=tail.chat_id,
            message_id=99,
            target_agent_id=CODEX.agent_id,
        )
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))

    def test_hub_decision_never_becomes_full_control_without_separate_local_consent(self) -> None:
        callback = self._preview()
        self._apply(callback)
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/other' WHERE topic_id=?",
                (self.topic.topic_id,),
            )

        full_control = self.state.preview_delivery_control("final_outbox", self.outbox.outbox_id)
        self.state.reconcile_delivery_control(
            "final_outbox",
            self.outbox.outbox_id,
            expected_snapshot=full_control.snapshot,
            accept_unconfirmed_delivery=True,
        )
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='root:/home/example/other' WHERE topic_id=?",
                (self.topic.topic_id,),
            )

    def test_hub_recovery_does_not_require_the_saved_result_provider_worker(self) -> None:
        """The Hub/outbox path may recover its FIFO hold without provider authority."""
        # A mixed rollout can use the external Hub/outbox while this older
        # saved result belongs to another locally managed provider consumer.
        self.hub.with_config(
            agents=(CODEX, ANTIGRAVITY), external_worker_agent_ids=(ANTIGRAVITY.agent_id,)
        )
        self.assertTrue(
            self.hub.service.handle_update(_topic_update(42, "/delivery", reply_to_message_id=901))
        )
        self.assertEqual(len(self.hub.telegram.sent), 1)
        self.assertIsNotNone(self.hub.telegram.markups[-1])

    def test_hub_recovery_requires_a_configured_numeric_hub_bot_identity(self) -> None:
        """A username alone must not authorize a durable remote capability."""
        self.hub.with_config(
            hub_bot=HubTelegramBot("example_hub_bot", self.hub.root.parent / "hub-token"),
        )
        self.assertTrue(
            self.hub.service.handle_update(_topic_update(42, "/delivery", reply_to_message_id=901))
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(any(self.hub.telegram.markups))

    def test_only_configured_owner_can_create_a_hub_preview(self) -> None:
        self.assertFalse(
            self.hub.service.handle_update(
                _topic_update(42, "/delivery", sender_id=900, reply_to_message_id=901)
            )
        )
        self.hub.with_config(
            acceptance_actors=(AcceptanceActor(900, CHAT_ID, THREAD_ID),),
        )
        self.assertFalse(
            self.hub.service.handle_update(
                _topic_update(43, "/delivery", sender_id=900, reply_to_message_id=901)
            )
        )
        self.assertEqual(self.hub.telegram.sent, [])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )

    def test_delivery_command_refuses_wrong_hub_origin_or_reply_without_productive_fallback(
        self,
    ) -> None:
        self.hub.service.ingress_identity = "codex"
        self.assertTrue(
            self.hub.service.handle_update(_topic_update(42, "/delivery", reply_to_message_id=901))
        )
        self.hub.service.ingress_identity = "hub"
        self.assertTrue(
            self.hub.service.handle_update(
                _topic_update(
                    43,
                    "/delivery",
                    reply_to_message_id=901,
                    reply_to_username="another_hub_bot",
                )
            )
        )
        self.assertTrue(self.hub.service.handle_update(_topic_update(44, "/delivery")))
        self.assertTrue(
            self.hub.service.handle_update(
                _topic_update(45, "/delivery", reply_to_message_id=901, reply_to_sender_id=701)
            )
        )
        hub_bot = self.hub.service.config.hub_bot
        assert hub_bot is not None
        self.assertTrue(
            self.hub.service.handle_update(
                _topic_update(
                    46,
                    "/delivery@" + "another_" + hub_bot.telegram_username,
                    reply_to_message_id=901,
                )
            )
        )
        self.assertEqual(self.hub.client.turns, 0)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )

    def test_delivery_command_must_be_literal_without_a_bot_suffix(self) -> None:
        """The narrow remote control accepts only the documented `/delivery` form."""
        hub_bot = self.hub.service.config.hub_bot
        assert hub_bot is not None
        self.assertTrue(
            self.hub.service.handle_update(
                _topic_update(
                    42,
                    "/delivery@" + hub_bot.telegram_username,
                    reply_to_message_id=901,
                )
            )
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.hub.client.turns, 0)

    def test_delivery_like_malformed_command_never_becomes_productive_input(self) -> None:
        before = self.state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0]
        self.assertTrue(self.hub.service.handle_update(_topic_update(42, "/delivery!")))
        self.assertEqual(
            self.state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0],
            before,
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )

    def test_callback_rechecks_owner_chat_topic_preview_message_and_origin(self) -> None:
        callback = self._preview()
        token = callback.removeprefix(CALLBACK_PREFIX)
        cases = ("owner", "chat", "topic", "message", "bot", "origin")
        for index, changed in enumerate(cases):
            with self.subTest(changed=changed), self.assertRaises(StateError):
                self.state.apply_telegram_delivery_hold_callback(
                    token,
                    owner_user_id=900 if changed == "owner" else OWNER_ID,
                    owner_user_ids=(OWNER_ID,),
                    chat_id=CHAT_ID - 1 if changed == "chat" else CHAT_ID,
                    thread_id=THREAD_ID + 1 if changed == "topic" else THREAD_ID,
                    callback_message_id=2 if changed == "message" else 1,
                    hub_bot_user_id=701 if changed == "bot" else 700,
                    callback_id=f"rejected-{index}",
                    callback_origin="codex" if changed == "origin" else CALLBACK_ORIGIN,
                    now=datetime.now(timezone.utc),
                )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_hub_ingress_rejects_nonowner_and_foreign_callback_message(self) -> None:
        """The callback needs both a configured owner and its Hub-bot preview origin."""
        callback = self._preview()
        self.assertTrue(
            self.hub.service.handle_update(_callback_update(1, callback, sender_id=900))
        )
        self.assertTrue(
            self.hub.service.handle_update(
                _callback_update(2, callback, message_username="another_hub_bot")
            )
        )
        self.assertTrue(
            self.hub.service.handle_update(_callback_update(3, callback, message_sender_id=701))
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_ambiguous_preview_send_creates_no_capability_or_automatic_retry(self) -> None:
        with patch.object(
            self.hub.telegram,
            "send_html",
            side_effect=TelegramError("fictional timeout", operation="send_message"),
        ) as send:
            self.assertTrue(
                self.hub.service.handle_update(
                    _topic_update(42, "/delivery", reply_to_message_id=901)
                )
            )
        send.assert_called_once()
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.state.get_telegram_outbox(self.outbox.outbox_id).status, "unknown")

    def test_positive_preview_without_durable_bind_is_unusable_and_not_resent(self) -> None:
        with patch.object(
            self.state,
            "bind_telegram_delivery_hold_preview",
            side_effect=StateError("fictional bind fault"),
        ):
            self.assertTrue(
                self.hub.service.handle_update(
                    _topic_update(42, "/delivery", reply_to_message_id=901)
                )
            )
        self.assertEqual(len(self.hub.telegram.sent), 1)
        markup = self.hub.telegram.markups[-1]
        assert isinstance(markup, dict)
        callback = markup["inline_keyboard"][0][0]["callback_data"]
        assert isinstance(callback, str)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )
        self.assertTrue(self.hub.service.handle_update(_callback_update(1, callback)))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(len(self.hub.telegram.sent), 1)

    def test_expired_and_stale_preview_fail_closed_without_consuming_a_fresh_callback(self) -> None:
        callback = self._preview()
        with self.assertRaises(StateError):
            self._apply(callback, now=datetime.now(timezone.utc) + timedelta(minutes=11))
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_malformed_persisted_token_lifetime_fails_closed(self) -> None:
        """Apply independently enforces the bounded lifetime retained in state."""
        callback = self._preview()
        with self.state._connection:
            self.state._connection.execute(
                "DROP TRIGGER telegram_delivery_hold_callback_tokens_no_update"
            )
            self.state._connection.execute(
                "UPDATE telegram_delivery_hold_callback_tokens SET expires_at=?",
                ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),),
            )
        with self.assertRaises(StateError):
            self._apply(callback)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_future_issued_preview_cannot_be_bound(self) -> None:
        """A clock-skewed preview must not create a button before it is valid."""
        preview = self.state.prepare_telegram_delivery_hold_preview(
            owner_user_id=OWNER_ID,
            owner_user_ids=(OWNER_ID,),
            hub_bot_user_id=700,
            chat_id=CHAT_ID,
            thread_id=THREAD_ID,
            notice_message_id=901,
            now=self.now,
        )
        future_issue = self.now + timedelta(minutes=1)
        future_preview = replace(
            preview,
            issued_at=future_issue.isoformat(),
            expires_at=(future_issue + timedelta(minutes=10)).isoformat(),
        )
        with self.assertRaises(StateError):
            self.state.bind_telegram_delivery_hold_preview(
                future_preview,
                owner_user_ids=(OWNER_ID,),
                preview_message_id=902,
                now=self.now,
            )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_callback_tokens"
            ).fetchone()[0],
            0,
        )

    def test_snapshot_cas_covers_all_parts_and_receipt_evidence(self) -> None:
        callback = self._preview()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox_parts SET telegram_html='Fictional changed part' "
                "WHERE outbox_id=?",
                (self.outbox.outbox_id,),
            )
        with self.assertRaises(StateError):
            self._apply(callback)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_new_receipt_evidence_invalidates_bound_callback(self) -> None:
        """A late receipt recorded after preview cannot be crossed by callback apply."""
        callback = self._preview()
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self.state._immediate_transaction():
            self.state.delivery._receipts.begin(
                "final",
                self.outbox.outbox_id,
                "fictional-late-receipt",
                1,
                recorded_at,
            )
            self.state.delivery._receipts.record(
                "final",
                self.outbox.outbox_id,
                "fictional-late-receipt",
                1,
                777,
                recorded_at,
            )
        with self.assertRaises(StateError):
            self._apply(callback)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )

    def test_same_owner_callback_is_idempotent_but_local_cli_cannot_relabel_it(self) -> None:
        callback = self._preview()
        first = self._apply(callback, callback_id="first-callback")
        repeated = self._apply(callback, callback_id="queued-replay")
        self.assertEqual(repeated, first)
        self.assertEqual(first.authority, "telegram_owner_callback")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT callback_id FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            "first-callback",
        )
        local = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.assertEqual(local.hold_status, "released_by_telegram_owner")
        with self.assertRaises(StateError):
            self.state.release_delivery_hold(
                self.outbox.outbox_id,
                expected_snapshot=local.snapshot,
                continue_without_confirmed_delivery=True,
            )

    def test_restart_after_preview_bind_and_concurrent_callbacks_record_one_decision(self) -> None:
        callback = self._preview()
        path = self.hub.config.state_path
        self.state.close()
        self.state = HubState.open_existing(path)
        self.hub.service.state = self.state

        barrier = threading.Barrier(2)
        records: list[object] = []
        errors: list[BaseException] = []

        def apply(callback_id: str) -> None:
            try:
                with closing(HubState.open_existing(path)) as other:
                    barrier.wait(timeout=5)
                    records.append(self._apply(callback, callback_id=callback_id, state=other))
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [
            threading.Thread(target=apply, args=(f"concurrent-{index}",)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], records[1])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            1,
        )

    def test_insert_fault_rolls_back_and_exact_callback_retry_succeeds(self) -> None:
        callback = self._preview()
        before = self._protected_evidence()
        with self.state._connection:
            self.state._connection.execute(
                "CREATE TRIGGER example_remote_delivery_hold_fault "
                "AFTER INSERT ON telegram_delivery_hold_remote_dispositions "
                "BEGIN SELECT RAISE(ABORT,'fictional fault'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._apply(callback)
        self.assertEqual(self._protected_evidence(), before)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_delivery_hold_remote_dispositions"
            ).fetchone()[0],
            0,
        )
        with self.state._connection:
            self.state._connection.execute("DROP TRIGGER example_remote_delivery_hold_fault")
        self._apply(callback)

    def test_malformed_remote_provenance_never_releases_fifo(self) -> None:
        callback = self._preview()
        self._apply(callback)
        tail = self._enqueue_tail()
        with self.state._connection:
            self.state._connection.execute(
                "DROP TRIGGER telegram_delivery_hold_remote_dispositions_no_update"
            )
            self.state._connection.execute(
                "UPDATE telegram_delivery_hold_remote_dispositions "
                "SET callback_message_id=callback_message_id+1"
            )
        self.assertEqual(
            self.state.preview_delivery_hold(self.outbox.outbox_id).hold_status,
            "disposition_binding_changed",
        )
        self.assertIsNone(self.state.lease_provider_job(CODEX.agent_id, "example-worker"))
        self.assertEqual(self.state.get_provider_job(tail.job_id).status, "queued")


if __name__ == "__main__":
    unittest.main()
