"""Characterize Controller command branches before extracting them.

Each test pins today's observable result of one dispatcher path: the return
value, the owner-visible reply and the writer ownership left in SQLite.
These branches were not exercised elsewhere; see stabilization stage 3.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.registry import ExecutionRootError
from hermes_codex_router.state import StateError
from tests.hub_service_harness import ANTIGRAVITY, CODEX, HubHarness, text_update
from tests.stop_fixtures import pending_stop


class ExternalSummary:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def publish_local_interval(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class CommandDispatchCharacterizationTests(unittest.TestCase):
    def harness(self, **kwargs: object) -> HubHarness:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        harness = HubHarness(Path(directory.name), **kwargs)  # type: ignore[arg-type]
        self.addCleanup(harness.close)
        return harness

    # /new

    def test_new_without_a_session_explains_and_changes_nothing(self) -> None:
        hub = self.harness()
        self.assertTrue(hub.send("/new"))
        self.assertIn("No active provider session exists yet.", hub.last_reply)

    def test_new_refuses_while_another_writer_owns_the_session(self) -> None:
        for writer, command in (("local", "/return"), ("terminal", "/release")):
            with self.subTest(writer=writer):
                hub = self.harness()
                hub.activate(CODEX, writer_mode=writer)
                self.assertTrue(hub.send("/new"))
                self.assertIn(f"Use {command} before resetting the session.", hub.last_reply)
                self.assertEqual(hub.session().writer_mode, writer)

    # /local

    def test_local_without_a_session_explains_and_changes_nothing(self) -> None:
        hub = self.harness()
        self.assertTrue(hub.send("/local"))
        self.assertIn("No active provider session exists yet.", hub.last_reply)

    def test_local_refuses_a_terminal_owned_session(self) -> None:
        hub = self.harness()
        hub.activate(CODEX, writer_mode="terminal")
        self.assertTrue(hub.send("/local"))
        self.assertIn("Use /release before taking the session local.", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "terminal")

    def test_local_requires_a_completed_provider_session(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        hub.activate(ANTIGRAVITY, provider_session_id=None)
        self.assertTrue(hub.send("/local"))
        self.assertIn("No completed provider session exists yet", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "telegram")

    def test_local_refuses_while_non_codex_work_is_queued(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        hub.activate(ANTIGRAVITY)
        self.assertTrue(hub.send("queued antigravity task"))
        self.assertTrue(hub.send("/local"))
        self.assertIn("Provider work is pending or being delivered", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "telegram")

    def test_local_transfers_ownership_and_prints_the_resume_command(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        hub.activate(ANTIGRAVITY)
        self.assertTrue(hub.send("/local"))
        self.assertIn("Local CLI now owns this provider session.", hub.last_reply)
        self.assertIn("Resume command:", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "local")

    # /return

    def test_return_without_a_session_explains_and_changes_nothing(self) -> None:
        hub = self.harness()
        self.assertTrue(hub.send("/return"))
        self.assertIn("No active provider session exists yet.", hub.last_reply)

    def test_return_refuses_terminal_and_already_telegram_owned_sessions(self) -> None:
        for writer, expected in (
            ("terminal", "Use /release for a managed terminal session."),
            (None, "Telegram already owns this provider session."),
        ):
            with self.subTest(writer=writer):
                hub = self.harness()
                hub.activate(CODEX, writer_mode=writer)
                self.assertTrue(hub.send("/return"))
                self.assertIn(expected, hub.last_reply)

    def test_codex_return_reports_changed_state_without_transfer(self) -> None:
        hub = self.harness()
        hub.activate(CODEX, writer_mode="local")
        with patch.object(
            type(hub.service.state), "return_codex_local_writer", side_effect=StateError("x")
        ):
            self.assertTrue(hub.send("/return"))
        self.assertIn("Ownership was not returned", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "local")

    def test_codex_return_is_model_free_and_names_no_optional_suffix(self) -> None:
        hub = self.harness()
        hub.activate(CODEX, writer_mode="local")
        self.assertTrue(hub.send("/return"))
        self.assertIn("Ownership returned to Telegram.", hub.last_reply)
        self.assertNotIn("paused for review", hub.last_reply)
        self.assertNotIn("archived", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "telegram")
        self.assertEqual(hub.client.turns, 0)

    def test_queued_non_codex_return_enqueues_a_summary_turn(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        hub.activate(ANTIGRAVITY, writer_mode="local")
        self.assertTrue(hub.send("/return"))
        jobs = hub.service.state.provider_jobs_for_topic(hub.topic().topic_id)
        self.assertEqual([job.agent_id for job in jobs], ["antigravity"])
        self.assertIn("Summarize only", jobs[0].payload_text)

    def test_inline_non_codex_return_publishes_the_local_interval(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY), dispatch_mode="inline")
        hub.activate(ANTIGRAVITY, writer_mode="local")
        external = ExternalSummary()
        hub.set_external_services({"antigravity": external})
        self.assertTrue(hub.send("/return"))
        self.assertEqual(len(external.calls), 1)
        self.assertEqual(external.calls[0]["project_id"], "example-project")
        self.assertEqual(hub.session().writer_mode, "telegram")

    def test_inline_non_codex_return_reports_summary_failure_after_transfer(self) -> None:
        for external, expected in (
            (None, "(ServiceError)."),
            (ExternalSummary(RuntimeError("secret detail")), "(RuntimeError)."),
        ):
            with self.subTest(expected=expected):
                hub = self.harness(agents=(CODEX, ANTIGRAVITY), dispatch_mode="inline")
                hub.activate(ANTIGRAVITY, writer_mode="local")
                hub.set_external_services({} if external is None else {"antigravity": external})
                self.assertTrue(hub.send("/return"))
                self.assertIn("but the local summary failed safely", hub.last_reply)
                self.assertTrue(hub.last_reply.endswith(expected))
                self.assertNotIn("secret detail", hub.last_reply)
                self.assertEqual(hub.session().writer_mode, "telegram")

    def test_inline_non_codex_return_keeps_local_owner_when_transfer_fails(self) -> None:
        cases = (
            (StateError("changed"), "session state changed. Retry /return."),
            (ExecutionRootError(), None),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                hub = self.harness(agents=(CODEX, ANTIGRAVITY), dispatch_mode="inline")
                hub.activate(ANTIGRAVITY, writer_mode="local")
                with patch(
                    "hermes_codex_router.service.resolve_topic_execution_root",
                    side_effect=error,
                ):
                    self.assertTrue(hub.send("/return"))
                if expected is not None:
                    self.assertIn(expected, hub.last_reply)
                else:
                    assert isinstance(error, ExecutionRootError)
                    self.assertEqual(hub.last_reply, error.public_message)
                self.assertEqual(hub.session().writer_mode, "local")

    def test_inline_non_codex_return_refuses_a_worktree_lane(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY), dispatch_mode="inline")
        hub.activate(ANTIGRAVITY, writer_mode="local")
        with patch.object(type(hub.service.state), "active_lane_for_topic", return_value=object()):
            self.assertTrue(hub.send("/return"))
        self.assertIn("Local summary is unavailable for a worktree lane", hub.last_reply)
        self.assertEqual(hub.session().writer_mode, "local")

    # /connect

    def test_connect_requires_the_hub_bot(self) -> None:
        hub = self.harness()
        self.assertTrue(hub.send("/connect"))
        self.assertIn("требует Hub bot", hub.last_reply)

    def test_connect_code_refusals_create_no_workflow(self) -> None:
        for text, expected in (
            ("/connect ONE TWO", "Использование: /connect КОД"),
            ("/connect NOT-A-CODE", "Код недействителен, истёк или относится к другому проекту."),
        ):
            with self.subTest(text=text):
                hub = self.harness()
                hub.with_config(hub_bot=HubTelegramBot("example_hub_bot", hub.root / "token"))
                self.assertTrue(hub.send(text))
                self.assertIn(expected, hub.last_reply)
                self.assertEqual(
                    hub.service.state.provider_jobs_for_topic(hub.topic().topic_id), ()
                )

    # control commands with attachments

    def test_control_command_with_an_attachment_is_refused_once(self) -> None:
        hub = self.harness()
        update = text_update(5, "")
        message = update["message"]
        assert isinstance(message, dict)
        message.pop("text")
        message["caption"] = "/status"
        message["document"] = {
            "file_id": "file-1",
            "file_unique_id": "unique-1",
            "file_name": "notes.txt",
            "mime_type": "text/plain",
            "file_size": 10,
        }
        self.assertTrue(hub.service.handle_update(update))
        self.assertIn("contains attachments", hub.last_reply)
        replies = len(hub.telegram.sent)
        self.assertFalse(hub.service.handle_update(update))
        self.assertEqual(len(hub.telegram.sent), replies)

    # emergency stop

    def test_stop_interrupts_a_running_mentioned_provider_and_its_queue(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        codex = hub.activate(CODEX)
        hub.activate(ANTIGRAVITY)
        state = hub.service.state
        topic = hub.topic()
        for message_id in (40, 41):
            state.enqueue_provider_job(
                idempotency_key=f"telegram:example:{message_id}",
                chat_id=topic.chat_id,
                message_id=message_id,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=codex.session_id,
                session_generation=codex.generation,
                model=codex.model,
                effort=codex.effort,
                payload_text="mentioned work",
            )
        leased = state.lease_provider_job("codex", "codex-worker")
        assert leased is not None and leased.lease_token is not None
        state.mark_provider_job_executing(leased.job_id, leased.lease_token)

        self.assertTrue(hub.send("stop", message_id=42))

        self.assertIn("Останавливаю активную работу; отменено задач в очереди: 1", hub.last_reply)
        self.assertIsNotNone(pending_stop(state, topic.topic_id, "codex"))
        self.assertIsNone(pending_stop(state, topic.topic_id, "antigravity"))
        self.assertEqual(hub.session().agent_id, "antigravity")

    # /agent

    def test_agent_without_a_name_offers_every_configured_agent(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        self.assertTrue(hub.send("/agent"))
        self.assertEqual(hub.last_reply, "Choose the active agent:")
        self.assertIn("agent:antigravity", str(hub.telegram.markups[-1]))

    def test_agent_with_extra_arguments_prints_usage(self) -> None:
        hub = self.harness()
        self.assertTrue(hub.send("/agent codex extra"))
        self.assertIn("Usage: /agent AGENT", hub.last_reply)

    def test_agent_switches_the_active_session_without_a_provider_call(self) -> None:
        hub = self.harness(agents=(CODEX, ANTIGRAVITY))
        hub.activate(CODEX)
        self.assertTrue(hub.send("/agent antigravity"))
        self.assertEqual(hub.session().agent_id, "antigravity")
        self.assertEqual(hub.client.turns, 0)


if __name__ == "__main__":
    unittest.main()
