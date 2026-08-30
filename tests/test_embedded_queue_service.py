from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.codex_appserver import CodexThread, RateLimits, TurnResult
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.service import ProjectHubService, QueueAcceptanceError
from hermes_codex_router.state import HubState


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_html(self, _chat_id: int, _thread_id: int, text: str, **_kwargs: object) -> int:
        self.sent.append(text)
        return len(self.sent)

    def answer_callback(self, _callback_id: str, _text: str = "") -> None:
        pass


class QueueClient:
    def __init__(
        self, *, block: bool = False, fail: bool = False, fail_limits: bool = False
    ) -> None:
        self.block = block
        self.fail = fail
        self.fail_limits = fail_limits
        self.entered = threading.Event()
        self.release = threading.Event()
        self.turn_threads: list[int] = []
        self.started_threads = 0

    def start_thread(self, **kwargs: object) -> CodexThread:
        self.started_threads += 1
        return CodexThread("thread-1", Path(str(kwargs["cwd"])), "gpt-5.6-sol", "openai")

    def resume_thread(self, **kwargs: object) -> CodexThread:
        return self.start_thread(**kwargs)

    def start_turn(self, **_kwargs: object) -> str:
        self.turn_threads.append(threading.get_ident())
        self.entered.set()
        if self.fail:
            raise RuntimeError("provider might have accepted the turn")
        return "turn-1"

    def wait_for_turn(self, _turn_id: str) -> TurnResult:
        if self.block:
            self.release.wait(3)
        return TurnResult("Visible answer", 1000, 100)

    def read_rate_limits(self) -> RateLimits:
        if self.fail_limits:
            raise RuntimeError("telemetry unavailable")
        return RateLimits(None, None)

    def close(self) -> None:
        pass


class FakeSupervisor:
    def __init__(self, client: QueueClient) -> None:
        self.client_value = client

    def client(self) -> QueueClient:
        return self.client_value

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def update(message_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "message_thread_id": 77,
            "is_topic_message": True,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Example"},
            "text": text,
        },
    }


class EmbeddedQueueServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        root = base / "project"
        (root / ".git").mkdir(parents=True)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example-project", -1001234567890),),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-5.6-sol",
                    "high",
                ),
            ),
            dispatch_mode="queue",
        )
        self.registry = ProjectRegistry(
            1, (base,), (Project("example-project", "Example", "Example", root),)
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def service(self, client: QueueClient) -> tuple[ProjectHubService, FakeTelegram]:
        value = ProjectHubService.__new__(ProjectHubService)
        value.config = self.config
        value.registry = self.registry
        value.state = HubState.open(self.config.state_path)
        value.agent = self.config.agents[0]
        telegram = FakeTelegram()
        value.telegram = cast(Any, telegram)
        value.supervisor = cast(Any, FakeSupervisor(client))
        value._codex_client = None
        value.usernames = {"codex": "example_codex_bot"}
        value.external_services = {}
        value._queue_stop = threading.Event()
        value._queue_thread = None
        return value, telegram

    def test_slow_provider_does_not_block_menu_and_runs_off_polling_thread(self) -> None:
        client = QueueClient(block=True)
        service, telegram = self.service(client)
        self.assertTrue(service.handle_update(update(1, "slow task")))
        service._start_embedded_queue_consumer()
        worker = service._queue_thread
        assert worker is not None
        self.assertTrue(client.entered.wait(1))
        self.assertTrue(service.handle_update(update(2, "/menu")))
        self.assertTrue(telegram.sent)
        self.assertNotEqual(client.turn_threads, [threading.get_ident()])
        client.release.set()
        service.close()
        self.assertFalse(worker.is_alive())

    def test_slow_provider_lease_is_maintained_by_separate_connection(self) -> None:
        client = QueueClient(block=True)
        service, _ = self.service(client)
        self.assertTrue(service.handle_update(update(1, "slow task")))
        worker = threading.Thread(target=service.run_embedded_queue_cycle)
        worker.start()
        self.assertTrue(client.entered.wait(1))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        deadline = datetime.now(timezone.utc) + timedelta(seconds=100)
        for _ in range(20):
            lease = service.state.provider_jobs_for_topic(topic.topic_id)[0].lease_expires_at
            if lease is not None and datetime.fromisoformat(lease) > deadline:
                break
            threading.Event().wait(0.05)
        else:
            self.fail("provider lease heartbeat did not extend the executing job")
        client.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        service.close()

    def test_duplicate_update_creates_one_job_and_one_turn(self) -> None:
        client = QueueClient()
        service, _ = self.service(client)
        self.assertTrue(service.handle_update(update(1, "one task")))
        self.assertFalse(service.handle_update(update(1, "one task")))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        self.assertEqual(len(service.state.provider_jobs_for_topic(topic.topic_id)), 1)
        self.assertTrue(service.run_embedded_queue_cycle())
        self.assertEqual(len(client.turn_threads), 1)
        service.close()

    def test_queued_work_survives_recreation_and_commands_do_not_enqueue(self) -> None:
        first, _ = self.service(QueueClient())
        self.assertTrue(first.handle_update(update(1, "durable task")))
        self.assertTrue(first.handle_update(update(2, "/menu")))
        topic = first.state.find_topic(-1001234567890, 77)
        assert topic is not None
        self.assertEqual(len(first.state.provider_jobs_for_topic(topic.topic_id)), 1)
        first.close()

        client = QueueClient()
        resumed, telegram = self.service(client)
        self.assertTrue(resumed.run_embedded_queue_cycle())
        self.assertEqual(len(client.turn_threads), 1)
        self.assertEqual(len(telegram.sent), 1)
        resumed.close()

    def test_large_visible_context_is_bounded_without_poisoning_ingress(self) -> None:
        service, _ = self.service(QueueClient())
        topic_message = update(1, "/menu")
        self.assertTrue(service.handle_update(topic_message))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        for index in range(8):
            service.state.record_visible_turn(
                topic.topic_id,
                agent_id=f"observer-{index}",
                provider="example",
                model="example-model",
                provider_session_id=f"session-{index}",
                user_excerpt="u" * 2000,
                response_excerpt="r" * 4000,
            )
        self.assertTrue(service.handle_update(update(2, "current request")))
        job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
        self.assertLessEqual(len(job.payload_text), 20000)
        self.assertTrue(job.payload_text.endswith("current request"))
        service.close()

    def test_return_transfers_local_writer_and_queues_summary_atomically(self) -> None:
        client = QueueClient()
        service, _ = self.service(client)
        self.assertTrue(service.handle_update(update(1, "initial task")))
        self.assertTrue(service.run_embedded_queue_cycle())
        self.assertTrue(service.handle_update(update(2, "/local")))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        local = service.state.active_session(topic.topic_id)
        assert local is not None
        self.assertEqual(local.writer_mode, "local")

        self.assertTrue(service.handle_update(update(3, "/return")))
        returned = service.state.active_session(topic.topic_id)
        assert returned is not None
        self.assertEqual(returned.writer_mode, "telegram")
        self.assertEqual(len(service.state.provider_jobs_for_topic(topic.topic_id)), 2)
        self.assertEqual(len(client.turn_threads), 1)
        self.assertTrue(service.run_embedded_queue_cycle())
        self.assertEqual(len(client.turn_threads), 2)
        service.close()

    def test_terminal_without_completed_session_does_not_call_provider(self) -> None:
        client = QueueClient()
        service, telegram = self.service(client)
        self.assertTrue(service.handle_update(update(1, "/terminal")))
        self.assertEqual(client.started_threads, 0)
        self.assertTrue(any("unavailable in queue mode" in item for item in telegram.sent))
        service.close()

    def test_cold_model_catalog_is_config_only_and_never_calls_provider(self) -> None:
        service, _ = self.service(QueueClient())

        def forbidden_discovery(_agent_id: str) -> object:
            raise AssertionError("provider discovery ran on controller path")

        service._discover_provider_models = forbidden_discovery  # type: ignore[method-assign]
        catalog = service._provider_catalog("codex", refresh=False)
        self.assertEqual(catalog.models[0].model_id, "gpt-5.6-sol")
        self.assertEqual(catalog.models[0].efforts, ("high",))
        service.close()

    def test_multi_target_message_is_rejected_explicitly_in_compatibility_mode(self) -> None:
        client = QueueClient()
        service, telegram = self.service(client)
        external = AgentDefinition(
            agent_id="example-open",
            display_name="Example Open",
            telegram_username="example_open_bot",
            runtime="opencode",
            token_file=None,
            terminal_enabled=False,
            managed_externally=False,
            default_model="provider-selected",
            default_effort="high",
            executable="opencode",
        )
        service.config = replace(service.config, agents=service.config.agents + (external,))
        service.usernames[external.agent_id] = external.telegram_username
        self.assertTrue(
            service.handle_update(
                update(1, "@example_codex_bot @example_open_bot compare approaches")
            )
        )
        self.assertTrue(any("one explicit provider target" in item for item in telegram.sent))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        self.assertEqual(service.state.provider_jobs_for_topic(topic.topic_id), ())
        service.close()

    def test_error_after_executing_is_indeterminate_without_retry(self) -> None:
        service, _ = self.service(QueueClient(fail=True))
        self.assertTrue(service.handle_update(update(1, "risky task")))
        self.assertTrue(service.run_embedded_queue_cycle())
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
        self.assertEqual(job.status, "indeterminate")
        self.assertEqual(job.error_class, "ambiguous_execution")
        self.assertFalse(service.run_embedded_queue_cycle())
        service.close()

    def test_failed_enqueue_is_replayed_without_a_preclaim(self) -> None:
        service, _ = self.service(QueueClient())
        original_enqueue = service.state.enqueue_provider_job

        def fail_enqueue(**_kwargs: object) -> object:
            raise RuntimeError("SQLite unavailable")

        service.state.enqueue_provider_job = fail_enqueue  # type: ignore[method-assign]
        with self.assertRaises(QueueAcceptanceError):
            service.handle_update(update(1, "replay me"))
        service.state.enqueue_provider_job = original_enqueue  # type: ignore[method-assign]
        self.assertTrue(service.handle_update(update(1, "replay me")))
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        self.assertEqual(len(service.state.provider_jobs_for_topic(topic.topic_id)), 1)
        service.close()

    def test_failed_enqueue_stops_batch_before_later_offsets(self) -> None:
        service, _ = self.service(QueueClient())
        handled: list[int] = []

        class BatchTelegram(FakeTelegram):
            def __init__(self) -> None:
                super().__init__()
                self.polls = 0

            def updates(
                self, *, offset: int | None = None, timeout: int = 50
            ) -> list[dict[str, object]]:
                del offset, timeout
                self.polls += 1
                if self.polls == 1:
                    return [{"update_id": 1}, {"update_id": 2}]
                raise KeyboardInterrupt

        def reject_first(item: dict[str, object]) -> bool:
            update_id = item["update_id"]
            assert isinstance(update_id, int)
            handled.append(update_id)
            raise QueueAcceptanceError("not committed")

        service.telegram = cast(Any, BatchTelegram())
        service.handle_update = reject_first  # type: ignore[method-assign]
        service._start_embedded_queue_consumer = lambda: None  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            service.run_forever()
        self.assertEqual(handled, [1])
        self.assertIsNone(service.state.get_bot_offset("codex"))
        service.close()

    def test_loop_error_uses_its_own_state_connection(self) -> None:
        service, _ = self.service(QueueClient())
        called = threading.Event()

        def fail_cycle() -> bool:
            called.set()
            service._queue_stop.set()
            raise RuntimeError("boom")

        service.run_embedded_queue_cycle = fail_cycle  # type: ignore[method-assign]
        service._start_embedded_queue_consumer()
        worker = service._queue_thread
        assert worker is not None
        self.assertTrue(called.wait(1))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        snapshot = service.state.status_snapshot()
        events = snapshot["runtime_events"]
        assert isinstance(events, list)
        self.assertTrue(any(item["code"] == "consumer_error" for item in events))
        service.close()

    def test_optional_rate_limit_telemetry_cannot_discard_a_completed_turn(self) -> None:
        service, _ = self.service(QueueClient(fail_limits=True))
        self.assertTrue(service.handle_update(update(1, "completed task")))
        self.assertTrue(service.run_embedded_queue_cycle())
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        self.assertEqual(
            service.state.provider_jobs_for_topic(topic.topic_id)[0].status, "completed"
        )
        service.close()
