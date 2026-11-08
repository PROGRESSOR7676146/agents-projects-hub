"""Schema56 rollback keeps accepted evidence but cannot mint remote recovery authority."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router import artifact_capabilities, migrations
from hermes_codex_router.cli import main
from hermes_codex_router.hub_config import AcceptanceActor, HubTelegramBot
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.task_notice_sender import deliver_task_notice
from tests.hub_service_harness import CHAT_ID, CODEX, OWNER_ID, THREAD_ID, HubHarness
from tests.schema_fixtures import project_historical_database


def _topic_update(
    message_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    sender_id: int = OWNER_ID,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": message_id,
        "message_thread_id": THREAD_ID,
        "is_topic_message": True,
        "from": {"id": sender_id, "is_bot": False},
        "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Example"},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "from": {"id": 700, "is_bot": True, "username": "example_hub_bot"},
        }
    return {"update_id": message_id, "message": message}


def _callback_update(
    message_id: int, data: str, *, callback_id: str = "rollback-callback"
) -> dict[str, object]:
    return {
        "update_id": 10_000 + message_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": OWNER_ID, "is_bot": False},
            "data": data,
            "message": {
                "message_id": message_id,
                "message_thread_id": THREAD_ID,
                "from": {"id": 700, "is_bot": True, "username": "example_hub_bot"},
                "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Example"},
            },
        },
    }


class Schema56SafeRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.hub = HubHarness(Path(self.temporary.name))
        self.addCleanup(self.hub.close)
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
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:940",
            chat_id=CHAT_ID,
            message_id=940,
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
        now = datetime.now(timezone.utc)
        self.assertEqual(
            self.state.delivery.prepare_delivery_wait_notices(
                self.state.task_notices,
                sender_agent_ids=(CODEX.agent_id,),
                now=now,
                hub_owner_recovery_enabled=False,
            ),
            1,
        )
        warning_bot = Mock()
        warning_bot.send_html.return_value = 901
        self.assertTrue(
            deliver_task_notice(
                self.state.task_notices, warning_bot, "example-sender", now=now
            ).delivered
        )
        self.hub.telegram.sent.clear()
        self.hub.telegram.markups.clear()

    def _protected_rows(self) -> dict[str, list[tuple[object, ...]]]:
        tables = (
            "provider_jobs",
            "provider_job_results",
            "telegram_outbox",
            "telegram_outbox_parts",
            "telegram_send_attempts",
            "telegram_send_receipts",
            "telegram_delivery_hold_callback_tokens",
            "telegram_delivery_hold_remote_dispositions",
        )
        return {
            table: [tuple(row) for row in self.state._connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }

    def _enqueue_tail(self, message_id: int = 941) -> str:
        job = self.state.get_provider_job(self.outbox.job_id)
        tail, _ = self.state.enqueue_provider_job(
            idempotency_key=f"telegram:{CHAT_ID}:{message_id}",
            chat_id=CHAT_ID,
            message_id=message_id,
            topic_id=job.topic_id,
            agent_id=CODEX.agent_id,
            session_id=job.session_id,
            session_generation=job.session_generation,
            provider_session_id=self.session.provider_session_id,
            model=job.model,
            effort=job.effort,
            payload_text="Fictional queued successor",
            context_watermark=None,
            handoff_id=None,
        )
        return tail.job_id

    def _create_issued_preview(self) -> str:
        with patch.object(artifact_capabilities, "REMOTE_DELIVERY_HOLD_RECOVERY", True):
            self.assertTrue(
                self.hub.service.handle_update(
                    _topic_update(942, "/delivery", reply_to_message_id=901)
                )
            )
        markup = self.hub.telegram.markups[-1]
        assert isinstance(markup, dict)
        callback = markup["inline_keyboard"][0][0]["callback_data"]
        assert isinstance(callback, str)
        return callback

    def test_enabled_configuration_is_readable_but_cannot_mint_remote_authority(self) -> None:
        self.assertFalse(self.hub.service.config.hub_delivery_hold_recovery_enabled)
        before = self._protected_rows()

        with patch.object(self.state, "prepare_telegram_delivery_hold_preview") as prepare:
            self.assertTrue(
                self.hub.service.handle_update(
                    _topic_update(942, "/delivery", reply_to_message_id=901)
                )
            )
        prepare.assert_not_called()
        self.assertTrue(all(markup is None for markup in self.hub.telegram.markups))
        self.assertEqual(self._protected_rows(), before)
        self.assertEqual(self.hub.client.turns, 0)
        self.assertIn("unavailable in this build", self.hub.last_reply)
        self.assertIn("delivery-hold", self.hub.last_reply)
        self.assertNotIn("Reply /delivery", self.hub.last_reply)

    def test_direct_state_facade_cannot_bypass_the_rollback_artifact_fence(self) -> None:
        before = self._protected_rows()

        with self.assertRaisesRegex(StateError, "unavailable in this build"):
            self.state.prepare_telegram_delivery_hold_preview(
                owner_user_id=OWNER_ID,
                owner_user_ids=(OWNER_ID,),
                hub_bot_user_id=700,
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                notice_message_id=901,
            )

        self.assertEqual(self._protected_rows(), before)

    def test_direct_state_cannot_bind_a_preview_created_before_switch_off(self) -> None:
        with patch.object(artifact_capabilities, "REMOTE_DELIVERY_HOLD_RECOVERY", True):
            preview = self.state.prepare_telegram_delivery_hold_preview(
                owner_user_id=OWNER_ID,
                owner_user_ids=(OWNER_ID,),
                hub_bot_user_id=700,
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                notice_message_id=901,
            )
        before = self._protected_rows()
        with self.assertRaisesRegex(StateError, "unavailable in this build"):
            self.state.bind_telegram_delivery_hold_preview(
                preview,
                owner_user_ids=(OWNER_ID,),
                preview_message_id=1,
            )
        self.assertEqual(self._protected_rows(), before)

    def test_existing_callback_cannot_create_a_decision_at_ingress_or_state(self) -> None:
        callback = self._create_issued_preview()
        before = self._protected_rows()
        with (
            patch.object(self.hub.telegram, "answer_callback") as answer,
            patch.object(self.state, "apply_telegram_delivery_hold_callback") as apply,
        ):
            self.assertTrue(self.hub.service.handle_update(_callback_update(1, callback)))
        apply.assert_not_called()
        self.assertIn("unavailable in this build", answer.call_args.args[1])
        self.assertIn("delivery-hold", answer.call_args.args[1])
        with self.assertRaisesRegex(StateError, "unavailable in this build"):
            self.state.apply_telegram_delivery_hold_callback(
                callback.removeprefix("dho:a:"),
                owner_user_id=OWNER_ID,
                owner_user_ids=(OWNER_ID,),
                chat_id=CHAT_ID,
                thread_id=THREAD_ID,
                callback_message_id=1,
                hub_bot_user_id=700,
                callback_id="example",
                callback_origin="hub",
            )
        self.assertEqual(self._protected_rows(), before)

    def test_authorized_nonowner_delivery_is_refused_without_provider_fallback(self) -> None:
        self.hub.with_config(acceptance_actors=(AcceptanceActor(900, CHAT_ID, THREAD_ID),))
        before = self._protected_rows()
        self.assertFalse(
            self.hub.service.handle_update(
                _topic_update(944, "/delivery", reply_to_message_id=901, sender_id=900)
            )
        )
        self.assertEqual(self._protected_rows(), before)
        self.assertEqual(self.hub.client.turns, 0)

    def test_existing_remote_disposition_still_releases_only_its_existing_fifo_barrier(
        self,
    ) -> None:
        tail_id = self._enqueue_tail()
        callback = self._create_issued_preview()
        with patch.object(artifact_capabilities, "REMOTE_DELIVERY_HOLD_RECOVERY", True):
            self.assertTrue(self.hub.service.handle_update(_callback_update(1, callback)))
        remote_before = self._protected_rows()

        path = self.hub.config.state_path
        self.state.close()
        self.state = HubState.open_existing(path)
        self.hub.service.state = self.state
        self.assertEqual(
            self.state.preview_delivery_hold(self.outbox.outbox_id).hold_status,
            "released_by_telegram_owner",
        )
        lease = self.state.lease_provider_job(CODEX.agent_id, "rollback-worker")
        assert lease is not None
        self.assertEqual(lease.job_id, tail_id)
        self.assertEqual(self.state.get_telegram_outbox(self.outbox.outbox_id).status, "unknown")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM telegram_send_attempts"
            ).fetchone()[0],
            0,
        )
        for table in (
            "provider_job_results",
            "telegram_outbox",
            "telegram_outbox_parts",
            "telegram_send_attempts",
            "telegram_send_receipts",
            "telegram_delivery_hold_callback_tokens",
            "telegram_delivery_hold_remote_dispositions",
        ):
            self.assertEqual(self._protected_rows()[table], remote_before[table], table)

    def test_local_owner_decision_still_works_without_remote_recovery(self) -> None:
        tail_id = self._enqueue_tail()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(["delivery-hold", str(self.hub.config.state_path), self.outbox.outbox_id]), 0
            )
        snapshot = json.loads(output.getvalue())["snapshot"]
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "delivery-hold",
                        str(self.hub.config.state_path),
                        self.outbox.outbox_id,
                        "--apply",
                        "--snapshot",
                        snapshot,
                        "--continue-without-confirmed-delivery",
                    ]
                ),
                0,
            )

        lease = self.state.lease_provider_job(CODEX.agent_id, "local-owner-worker")
        assert lease is not None
        self.assertEqual(lease.job_id, tail_id)
        self.assertEqual(self.state.get_telegram_outbox(self.outbox.outbox_id).status, "unknown")

    def test_populated_schema55_migrates_to_56_and_reopens_with_retained_state(self) -> None:
        legacy = self.hub.config.state_path.with_name("fictional-schema55.db")
        project_historical_database(self.hub.config.state_path, legacy, 55)

        result = migrations.migrate_database(legacy)

        self.assertEqual((result.previous_version, result.current_version), (55, 56))
        with closing(HubState.open_existing(legacy)) as reopened:
            self.assertEqual(reopened.schema_version, 56)
            self.assertEqual(
                reopened.get_telegram_outbox(self.outbox.outbox_id).status,
                "unknown",
            )
            self.assertEqual(
                reopened.preview_delivery_hold(self.outbox.outbox_id).hold_status,
                "outstanding",
            )
        with closing(HubState.open_existing(legacy)) as restarted:
            self.assertEqual(restarted.schema_version, 56)

    def test_future_schema57_state_is_a_concrete_compatibility_blocker(self) -> None:
        """A result-inspection schema increment needs its own compatible rollback."""
        with self.state._connection:
            self.state._connection.execute("PRAGMA user_version = 57")

        with self.assertRaisesRegex(StateError, "state_schema_unsupported"):
            HubState.open_existing(self.hub.service.config.state_path)


if __name__ == "__main__":
    unittest.main()
