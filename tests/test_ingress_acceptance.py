from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.telegram import TelegramError
from tests.fault_matrix_support import FaultMatrixHarness, RecordingBot


class ControlledStop:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float | None] = []

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.stopped


class ScriptedTelegram(RecordingBot):
    def __init__(self, service: ProjectHubService, batches: list[list[dict[str, object]]]) -> None:
        super().__init__()
        self.service = service
        self.batches = batches
        self.poll_offsets: list[int | None] = []

    def updates(self, *, offset: int | None, timeout: int) -> list[dict[str, object]]:
        del timeout
        self.poll_offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        self.service.stop()
        return []


class FailingActionTelegram(ScriptedTelegram):
    def send_chat_action(self, _chat_id: int, _thread_id: int, _action: str = "typing") -> None:
        raise TelegramError(
            "fictional chat action failure",
            operation="chat_action",
            failure_class="network",
        )


class IngressAcceptanceTests(unittest.TestCase):
    def run_batches(
        self,
        service: ProjectHubService,
        batches: list[list[dict[str, object]]],
        *,
        telegram_type: type[ScriptedTelegram] = ScriptedTelegram,
    ) -> tuple[ScriptedTelegram, ControlledStop]:
        stop = ControlledStop()
        service._stop = cast(Any, stop)
        telegram = telegram_type(service, batches)
        service.telegram = cast(Any, telegram)
        service.run_forever()
        return telegram, stop

    @staticmethod
    def input_rows(service: ProjectHubService) -> list[tuple[int, int, str]]:
        return [
            (int(row[0]), int(row[1]), str(row[2]))
            for row in service.state._connection.execute(
                """SELECT message_id, part_index, input_text
                   FROM provider_job_inputs ORDER BY message_id, part_index"""
            ).fetchall()
        ]

    def test_transient_satellite_failure_is_retried_before_offset_advances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            message = harness.update(
                141,
                841,
                "@example_opencode_bot fictional task",
            )
            first = harness.controller()
            try:
                with patch.object(
                    first.state,
                    "ensure_satellite",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    telegram, stop = self.run_batches(first, [[message]])

                self.assertIsNone(first.state.get_bot_offset("hub"))
                self.assertEqual(telegram.poll_offsets, [None, None])
                self.assertEqual(stop.waits, [1])
                self.assertEqual(
                    first.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_jobs"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    first.state._connection.execute(
                        "SELECT COUNT(*) FROM observed_messages"
                    ).fetchone()[0],
                    0,
                )
            finally:
                first.close()

            resumed = harness.controller()
            try:
                self.run_batches(resumed, [[message]])
                topic = resumed.state.find_topic(harness.chat_id, 841)
                assert topic is not None
                jobs = resumed.state.provider_jobs_for_topic(topic.topic_id)
                self.assertEqual(resumed.state.get_bot_offset("hub"), 142)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].agent_id, "opencode")
                self.assertEqual(
                    resumed.state._connection.execute(
                        "SELECT COUNT(*) FROM observed_messages"
                    ).fetchone()[0],
                    1,
                )
            finally:
                resumed.close()

    def test_transient_primary_session_creation_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            message = harness.update(151, 851, "fictional primary task")
            first = harness.controller()
            try:
                with patch.object(
                    first.state,
                    "activate_agent",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    self.run_batches(first, [[message]])
                self.assertIsNone(first.state.get_bot_offset("hub"))
                self.assertEqual(
                    first.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_jobs"
                    ).fetchone()[0],
                    0,
                )
            finally:
                first.close()

            resumed = harness.controller()
            try:
                self.run_batches(resumed, [[message]])
                self.assertEqual(resumed.state.get_bot_offset("hub"), 152)
                self.assertEqual(harness.one_job(851).agent_id, "codex")
                self.assertEqual(len(self.input_rows(resumed)), 1)
            finally:
                resumed.close()

    def test_failed_early_batch_item_blocks_later_offset_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            first_message = harness.update(161, 861, "@example_opencode_bot first fictional part")
            second_message = harness.update(162, 861, "@example_opencode_bot second fictional part")
            first = harness.controller()
            try:
                with patch.object(
                    first.state,
                    "ensure_satellite",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    self.run_batches(first, [[first_message, second_message]])
                self.assertIsNone(first.state.get_bot_offset("hub"))
                self.assertEqual(
                    first.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_jobs"
                    ).fetchone()[0],
                    0,
                )
            finally:
                first.close()

            resumed = harness.controller()
            try:
                self.run_batches(resumed, [[first_message, second_message]])
                topic = resumed.state.find_topic(harness.chat_id, 861)
                assert topic is not None
                jobs = resumed.state.provider_jobs_for_topic(topic.topic_id)
                self.assertEqual(resumed.state.get_bot_offset("hub"), 163)
                self.assertEqual(
                    [(job.message_id, job.topic_sequence) for job in jobs],
                    [(161, 1), (162, 2)],
                )
                self.assertEqual(
                    self.input_rows(resumed),
                    [
                        (161, 1, "first fictional part"),
                        (162, 1, "second fictional part"),
                    ],
                )
            finally:
                resumed.close()

    def test_enqueue_fault_and_offset_fault_converge_without_duplicate_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            message = harness.update(171, 871, "@example_opencode_bot fictional commit boundary")
            before_commit = harness.controller()
            try:
                with patch.object(
                    before_commit.state,
                    "enqueue_or_append_provider_job",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    self.run_batches(before_commit, [[message]])
                self.assertIsNone(before_commit.state.get_bot_offset("hub"))
                self.assertEqual(
                    before_commit.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_jobs"
                    ).fetchone()[0],
                    0,
                )
            finally:
                before_commit.close()

            after_commit = harness.controller()
            try:
                with patch.object(
                    after_commit.state,
                    "set_bot_offset",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    self.run_batches(after_commit, [[message]])
                self.assertIsNone(after_commit.state.get_bot_offset("hub"))
                self.assertEqual(len(self.input_rows(after_commit)), 1)
            finally:
                after_commit.close()

            resumed = harness.controller()
            try:
                self.run_batches(resumed, [[message]])
                topic = resumed.state.find_topic(harness.chat_id, 871)
                assert topic is not None
                self.assertEqual(resumed.state.get_bot_offset("hub"), 172)
                self.assertEqual(len(resumed.state.provider_jobs_for_topic(topic.topic_id)), 1)
                self.assertEqual(len(self.input_rows(resumed)), 1)
            finally:
                resumed.close()

    def test_post_acceptance_typing_failure_does_not_lose_or_duplicate_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            message = harness.update(181, 881, "@example_opencode_bot fictional typing boundary")
            service = harness.controller()
            try:
                self.run_batches(
                    service, [[message], [message]], telegram_type=FailingActionTelegram
                )
                topic = service.state.find_topic(harness.chat_id, 881)
                assert topic is not None
                self.assertEqual(service.state.get_bot_offset("hub"), 182)
                self.assertEqual(len(service.state.provider_jobs_for_topic(topic.topic_id)), 1)
                self.assertEqual(len(self.input_rows(service)), 1)
            finally:
                service.close()

    def test_diagnostic_fault_cannot_ack_failed_admission_or_remove_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            message = harness.update(
                191, 891, "@example_opencode_bot fictional diagnostic boundary"
            )
            service = harness.controller()
            original_record = service.state.record_runtime_event

            def fail_admission_event(component: str, severity: str, code: str, detail: str) -> None:
                if code == "queue_enqueue_error":
                    raise sqlite3.OperationalError("database is locked")
                original_record(component, severity, code, detail)

            try:
                with (
                    patch.object(
                        service.state,
                        "ensure_satellite",
                        side_effect=sqlite3.OperationalError("database is locked"),
                    ),
                    patch.object(service.state, "record_runtime_event", fail_admission_event),
                ):
                    telegram, stop = self.run_batches(service, [[message]])
                self.assertIsNone(service.state.get_bot_offset("hub"))
                self.assertEqual(telegram.poll_offsets, [None, None])
                self.assertEqual(stop.waits, [1])
                self.assertEqual(
                    service.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_jobs"
                    ).fetchone()[0],
                    0,
                )
            finally:
                service.close()

    def test_terminal_and_ignored_updates_advance_without_provider_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            malformed: dict[str, object] = {"update_id": 201}
            unauthorized = harness.update(202, 901, "fictional unauthorized task")
            cast(dict[str, Any], unauthorized["message"])["from"] = {
                "id": 99,
                "is_bot": False,
            }
            forwarded = harness.update(203, 901, "fictional passive quote")
            cast(dict[str, Any], forwarded["message"])["forward_origin"] = {
                "type": "hidden_user",
                "sender_user_name": "Example Person",
                "date": 1790000000,
            }
            command = harness.update(204, 901, "/status")
            empty_mention = harness.update(205, 901, "@example_opencode_bot")
            service = harness.controller()
            try:
                self.run_batches(
                    service,
                    [[malformed, unauthorized, forwarded, command, empty_mention]],
                )
                self.assertEqual(service.state.get_bot_offset("hub"), 206)
                self.assertEqual(
                    service.state._connection.execute(
                        "SELECT COUNT(*) FROM provider_jobs"
                    ).fetchone()[0],
                    0,
                )
            finally:
                service.close()

    def test_inline_failure_after_possible_invocation_is_not_productively_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            message = harness.update(211, 911, "fictional inline task")
            service = harness.controller(ingress_identity="codex")
            service.config = replace(
                harness.config,
                hub_bot=None,
                dispatch_mode="inline",
                queue_runtime="embedded",
                outbox_runtime="controller",
            )
            productive_calls = 0

            def fail_after_possible_invocation(**_kwargs: object) -> str:
                nonlocal productive_calls
                productive_calls += 1
                raise RuntimeError("fictional indeterminate provider failure")

            service._run_codex_turn = fail_after_possible_invocation  # type: ignore[method-assign]
            service._start_embedded_queue_consumer = lambda: None  # type: ignore[method-assign]
            service._start_controller_outbox_delivery = lambda: None  # type: ignore[method-assign]
            try:
                self.run_batches(service, [[message], [message]])
                self.assertEqual(service.state.get_bot_offset("codex"), 212)
                self.assertEqual(productive_calls, 1)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
