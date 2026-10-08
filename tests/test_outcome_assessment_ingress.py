"""Assessment controls are never provider requests or alternate writer authority."""

from __future__ import annotations

import copy
import sqlite3
import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.catalog_refresh import CatalogRefreshResult
from hermes_codex_router.diagnostics import Check, run_doctor
from hermes_codex_router.external_service import ExternalAgentService
from hermes_codex_router.hub_config import AcceptanceActor, HubTelegramBot
from hermes_codex_router.monitoring import run_monitor_once
from hermes_codex_router.service import QueueAcceptanceError
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.terminal_runtime import TerminalRuntime
from tests import test_embedded_queue_service as fixtures
from tests.delivery_fixture import complete_final_delivery


class OutcomeAssessmentIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.EmbeddedQueueServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        config = self.fixture.config
        self.fixture.config = replace(
            config,
            hub_bot=HubTelegramBot("example_hub_bot", config.state_path.parent / "token"),
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        self.client = fixtures.QueueClient()
        self.service, self.telegram = self.fixture.service(self.client)
        self.service.ingress_identity = "hub"
        self.addCleanup(lambda: self.service.state.close())
        self.topic = self.service.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        self.session = self.service.state.activate_agent(
            self.topic.topic_id, "codex", "example-model", "high"
        )
        self.job, _ = self.service.state.enqueue_provider_job(
            idempotency_key="example-assess-result",
            chat_id=self.topic.chat_id,
            message_id=1,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Example task",
        )
        lease = self.service.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.service.state.mark_provider_job_executing(self.job.job_id, lease.lease_token)
        self.service.state.commit_provider_result(
            self.job.job_id,
            lease.lease_token,
            visible_response="Example result",
            sender_agent_id="codex",
            telegram_html="Example result",
        )
        outbox = self.service.state.lease_telegram_outbox("codex", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        complete_final_delivery(
            self.service.state, outbox.outbox_id, outbox.lease_token, telegram_message_id=101
        )

    @staticmethod
    def update(number: int = 50, text: str = "/assess accepted Example checked"):
        result = fixtures.update(number, text)
        cast(dict[str, Any], result["message"])["reply_to_message"] = {
            "message_id": 101,
            "from": {"id": 987, "is_bot": True, "username": "example_codex_bot"},
        }
        return result

    def records(self):
        return self.service.state._connection.execute(
            "SELECT * FROM outcome_assessment_dispositions"
        ).fetchall()

    def assert_no_execution(self) -> None:
        self.assertEqual(len(self.service.state.provider_jobs_for_topic(self.topic.topic_id)), 1)
        self.assertEqual(self.client.started_threads, 0)
        self.assertEqual(self.client.turn_threads, [])
        self.assertEqual(self.service.state.get_session(self.session.session_id), self.session)

    def test_reply_reserved_before_routing_material_cleanup_and_session_preparation(self) -> None:
        with (
            patch.object(self.service, "_topic", side_effect=AssertionError("no topic mutation")),
            patch.object(
                self.service, "_discard_pending_materials", side_effect=AssertionError("no cleanup")
            ),
            patch.object(
                self.service,
                "_discard_terminal_materials",
                side_effect=AssertionError("no cleanup"),
            ),
            patch(
                "hermes_codex_router.service.decide_ingress",
                side_effect=AssertionError("no routing"),
            ),
        ):
            self.assertTrue(self.service.handle_update(self.update()))
        self.assertEqual(self.records()[0]["disposition"], "applied")
        self.assertEqual(self.telegram.sent, [])
        self.assert_no_execution()

    def test_malformed_caption_quote_and_material_are_durable_refusals_without_download(
        self,
    ) -> None:
        for number, variant in enumerate(
            ("malformed", "caption", "quote", "document", "unavailable"), 50
        ):
            incoming = self.update(number)
            message = cast(dict[str, Any], incoming["message"])
            if variant == "malformed":
                message["text"] = "/assess accepted"
            elif variant == "caption":
                message["caption"] = message.pop("text")
            elif variant == "quote":
                message["quote"] = {"text": "Example quoted context"}
            elif variant == "document":
                message["document"] = {
                    "file_id": "example-file",
                    "file_unique_id": "example-unique",
                    "file_name": "example.txt",
                    "file_size": 4,
                }
            else:
                message["video"] = {"file_id": "example-video"}
            with (
                patch(
                    "hermes_codex_router.controller_admission.receive_incoming_materials",
                    side_effect=AssertionError("no downloads"),
                ),
                patch(
                    "hermes_codex_router.service.receive_incoming_materials",
                    side_effect=AssertionError("no downloads"),
                ),
            ):
                self.assertTrue(self.service.handle_update(incoming), variant)
        self.assertEqual([r["disposition"] for r in self.records()], ["refused"] * 5)
        self.assert_no_execution()

    def test_exact_restart_duplicate_and_changed_input_keep_original_decision(self) -> None:
        self.assertTrue(self.service.handle_update(self.update()))
        self.service.state.close()
        self.service.state = HubState.open(
            self.service.config.state_path, codex_permission_profile=None
        )
        self.assertFalse(self.service.handle_update(self.update()))
        self.assertFalse(
            self.service.handle_update(self.update(text="/assess rework Example changed"))
        )
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["decision"], "accepted")
        self.assert_no_execution()

    def test_poison_reasons_are_durably_refused_without_input_retry_or_execution(self) -> None:
        for number, reason in enumerate(("\x00", "\x01bad", "bad\x1f", "\ud800", "\udfff"), 50):
            with self.subTest(reason=repr(reason)):
                incoming = self.update(number, f"/assess accepted {reason}")
                self.assertTrue(self.service.handle_update(incoming))
                stored = self.records()[-1]
                self.assertEqual(stored["disposition"], "refused")
                self.assertIsNone(stored["reason"])
                self.assertTrue(
                    self.service.state.message_already_observed(self.topic.chat_id, number)
                )
                self.assertFalse(self.service.handle_update(incoming))
        self.assertEqual(len(self.records()), 5)
        self.assertEqual(
            self.service.state._connection.execute(
                "SELECT COUNT(*) FROM task_lifecycle_notices"
            ).fetchone()[0],
            5,
        )
        self.assert_no_execution()

    def test_unpaired_surrogate_in_raw_metadata_is_fingerprinted_without_retry(self) -> None:
        incoming = self.update()
        cast(dict[str, Any], incoming["message"])["example_metadata"] = "\ud800"
        self.assertTrue(self.service.handle_update(incoming))
        stored = self.records()[0]
        self.assertEqual(stored["disposition"], "applied")
        self.assertFalse(self.service.handle_update(incoming))
        changed = copy.deepcopy(incoming)
        cast(dict[str, Any], changed["message"])["example_metadata"] = "\udfff"
        with patch.object(
            self.service.state,
            "record_outcome_assessment",
            wraps=self.service.state.record_outcome_assessment,
        ) as record:
            self.assertFalse(self.service.handle_update(changed))
            self.assertNotEqual(stored["input_fingerprint"], record.call_args.args[0].fingerprint())
        self.assertEqual(len(self.records()), 1)
        self.assert_no_execution()

    def test_subjectless_ack_is_safe_in_status_doctor_and_monitor_even_when_unknown(self) -> None:
        self.assertTrue(self.service.handle_update(self.update()))
        notice = self.service.state.task_notices.lease_notice(
            "example-sender", now=fixtures.datetime.now(fixtures.timezone.utc)
        )
        assert notice is not None and notice.lease_token is not None
        self.assertIsNone(notice.job_id)
        self.assertIsNone(notice.stop_request_id)
        self.assertIsNotNone(notice.assessment_disposition_id)
        baseline = self.service.state.status_snapshot()["reliability"]
        for status in ("leased", "unknown"):
            with self.subTest(status=status):
                if status == "unknown":
                    now = fixtures.datetime.now(fixtures.timezone.utc)
                    self.service.state.task_notices.begin_send(
                        notice.notice_id, notice.lease_token, now=now
                    )
                    self.service.state.task_notices.mark_send_unknown(
                        notice.notice_id, notice.lease_token, error_code="example-timeout", now=now
                    )
                with (
                    patch(
                        "hermes_codex_router.diagnostics._command",
                        return_value=Check("example-command", True, "example"),
                    ),
                    patch(
                        "hermes_codex_router.diagnostics._socket_check",
                        return_value=Check("example-socket", True, "example"),
                    ),
                    patch(
                        "hermes_codex_router.diagnostics.probe_codex_config_proxy",
                        return_value=Mock(ok=True, detail="example"),
                    ),
                    patch.object(TerminalRuntime, "launcher_available", return_value=True),
                    patch.object(TerminalRuntime, "launcher_program", return_value=None),
                    patch(
                        "hermes_codex_router.monitoring.refresh_provider_catalogs",
                        return_value=CatalogRefreshResult((), (), {}),
                    ),
                    patch("hermes_codex_router.monitoring._hermes_health", return_value=None),
                    patch("hermes_codex_router.monitoring._telegram_access", return_value={}),
                    patch(
                        "hermes_codex_router.monitoring.project_runtime_health", return_value=None
                    ),
                ):
                    doctor = run_doctor(self.service.config)
                    monitor = run_monitor_once(self.service.config, notify=False)
                self.assertTrue(doctor["ok"])
                self.assertTrue(monitor["ok"])
                self.assertEqual(monitor["delivered"], [])
                self.assertEqual(monitor["reliability"], baseline)
                self.assertEqual(self.service.state.status_snapshot()["reliability"], baseline)
                self.assertEqual(
                    self.service.state.task_notices.get_notice(notice.notice_id).status, status
                )
                self.assertEqual(self.records()[0]["decision"], "accepted")
        self.assert_no_execution()

    def test_source_digest_retains_normalized_away_quote_and_attachment_differences(self) -> None:
        for number, variant in enumerate(("quote", "attachment"), 50):
            incoming = self.update(number)
            raw = cast(dict[str, Any], incoming["message"])
            if variant == "quote":
                raw["quote"] = {"text": "x" * 4000 + "Original tail"}
            else:
                raw["document"] = {
                    "file_id": "example-file",
                    "file_unique_id": "example-unique",
                    "file_name": "example.txt",
                    "file_size": 4,
                    "example_unmodeled_metadata": "original",
                }
            self.assertTrue(self.service.handle_update(incoming))
            with patch.object(
                self.service.state,
                "record_outcome_assessment",
                wraps=self.service.state.record_outcome_assessment,
            ) as record:
                changed = copy.deepcopy(incoming)
                changed_raw = cast(dict[str, Any], changed["message"])
                if variant == "quote":
                    changed_raw["quote"]["text"] = "x" * 4000 + "Changed tail"
                else:
                    changed_raw["document"]["example_unmodeled_metadata"] = "changed"
                self.assertFalse(self.service.handle_update(changed))
                changed_digest = record.call_args.args[0].fingerprint()
            stored = self.records()[-1]
            self.assertNotEqual(stored["input_fingerprint"], changed_digest)
            incoming["update_id"] = 500 + number
            with patch.object(
                self.service.state,
                "record_outcome_assessment",
                wraps=self.service.state.record_outcome_assessment,
            ) as duplicate:
                self.assertFalse(self.service.handle_update(incoming))
                self.assertEqual(
                    stored["input_fingerprint"], duplicate.call_args.args[0].fingerprint()
                )
        self.assertEqual(len(self.records()), 2)
        self.assert_no_execution()

    def test_sql_and_notice_faults_are_retryable_without_a_partial_disposition(self) -> None:
        for target, method, failure in (
            (
                self.service,
                "_project_binding_for_chat",
                sqlite3.OperationalError("example binding fault"),
            ),
            (
                self.service.state.task_notices,
                "prepare_notice_in_transaction",
                StateError("example notice fault"),
            ),
        ):
            with (
                patch.object(target, method, side_effect=failure),
                self.assertRaises(QueueAcceptanceError),
            ):
                self.service.handle_update(self.update())
            self.assertEqual(self.records(), [])
            self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 50))
        self.assertTrue(self.service.handle_update(self.update()))
        self.assert_no_execution()

    def test_noncentral_provider_cannot_steal_input_before_or_after_hub(self) -> None:
        self.service.ingress_identity = "codex"
        self.assertFalse(self.service.handle_update(self.update()))
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 50))
        self.service.ingress_identity = "hub"
        self.assertTrue(self.service.handle_update(self.update()))
        self.service.ingress_identity = "codex"
        self.assertFalse(self.service.handle_update(self.update()))
        self.assertEqual(len(self.records()), 1)
        self.assert_no_execution()

    def test_private_reserved_control_does_not_enter_hub_workflows_or_create_topic(self) -> None:
        incoming = self.update()
        message = cast(dict[str, Any], incoming["message"])
        message["chat"] = {"id": 42, "type": "private"}
        message.pop("message_thread_id")
        with patch.object(
            self.service, "_handle_hub_direct", side_effect=AssertionError("no workflow")
        ):
            self.assertTrue(self.service.handle_update(incoming))
        self.assertIsNone(self.service.state.find_topic(42, 1))
        self.assertIn("project topic", self.telegram.sent[-1])
        self.assertEqual(self.records(), [])
        self.assert_no_execution()

    def test_forwarded_assessment_keeps_passive_semantics(self) -> None:
        incoming = self.update()
        cast(dict[str, Any], incoming["message"])["forward_origin"] = {"type": "user"}
        self.assertTrue(self.service.handle_update(incoming))
        self.assertEqual(self.records(), [])
        self.assert_no_execution()

    def test_unsupported_owner_mode_refuses_once_without_execution(self) -> None:
        self.service.config = replace(self.service.config, outbox_runtime="controller")
        self.assertTrue(self.service.handle_update(self.update()))
        self.assertFalse(self.service.handle_update(self.update()))
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("unavailable", self.telegram.sent[0])
        self.assertEqual(self.records(), [])
        self.assert_no_execution()

    def test_untrusted_sender_and_scoped_actor_have_no_assessment_authority(self) -> None:
        incoming = self.update()
        message = cast(dict[str, Any], incoming["message"])
        message["from"] = {"id": 43, "is_bot": False}
        self.assertFalse(self.service.handle_update(incoming))
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 50))
        self.service.config = replace(
            self.service.config,
            acceptance_actors=(AcceptanceActor(43, self.topic.chat_id, self.topic.thread_id),),
        )
        self.assertTrue(self.service.handle_update(incoming))
        self.assertFalse(self.service.handle_update(incoming))
        self.assertIn("Only a configured owner", self.telegram.sent[-1])
        self.assertEqual(self.records(), [])
        self.assert_no_execution()

    def test_legacy_external_direct_reserves_control_without_topic_or_provider(self) -> None:
        endpoint = ExternalAgentService.__new__(ExternalAgentService)
        endpoint.config, endpoint.state = self.service.config, self.service.state
        endpoint.agent, endpoint.telegram = self.service.agent, cast(Any, self.telegram)
        endpoint.direct_messages_only = True
        incoming = self.update()
        message = cast(dict[str, Any], incoming["message"])
        message["chat"] = {"id": 42, "type": "private"}
        message.pop("message_thread_id")
        with patch.object(endpoint, "_direct_topic", side_effect=AssertionError("no session")):
            self.assertTrue(endpoint.handle_update(incoming))
        self.assertIsNone(self.service.state.find_topic(42, 1))
        self.assertEqual(self.records(), [])
        endpoint.direct_messages_only = False
        self.assertFalse(endpoint.handle_update(self.update()))
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 50))
        self.assert_no_execution()
