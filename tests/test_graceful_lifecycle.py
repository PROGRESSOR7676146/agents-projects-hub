from __future__ import annotations

import signal
import threading
import unittest
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.external_service import ExternalAgentService
from hermes_codex_router.lifecycle import stop_on_signals
from hermes_codex_router.service import ProjectHubService, ServiceError


class Stoppable:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class GracefulLifecycleTests(unittest.TestCase):
    def test_signal_handlers_only_request_stop_and_restore_previous_handlers(self) -> None:
        component = Stoppable()
        previous = {
            signal.SIGINT: signal.getsignal(signal.SIGINT),
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        }

        with stop_on_signals(component):
            for signum in (signal.SIGINT, signal.SIGTERM):
                handler = signal.getsignal(signum)
                self.assertTrue(callable(handler))
                handler(signum, None)  # type: ignore[misc]

        self.assertEqual(component.stop_calls, 2)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous[signal.SIGINT])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous[signal.SIGTERM])

    def test_non_main_thread_does_not_install_process_signal_handlers(self) -> None:
        component = Stoppable()
        installed: list[tuple[int, Any]] = []

        def run() -> None:
            with patch("hermes_codex_router.lifecycle.signal.signal") as install:
                with stop_on_signals(component):
                    installed.extend(install.call_args_list)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(installed, [])
        self.assertEqual(component.stop_calls, 0)

    def test_every_long_running_cli_command_installs_the_stop_handler(self) -> None:
        instances: list[Any] = []

        class Component:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.stopped = False
                self.closed = False
                instances.append(self)

            def stop(self) -> None:
                self.stopped = True

            def run_forever(self, **_kwargs: object) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)  # type: ignore[misc]

            def close(self) -> None:
                self.closed = True

        commands = (
            ("controller", ["controller", "example.json"], "ProjectHubService"),
            ("serve", ["serve", "example.json"], "ProjectHubService"),
            (
                "direct serve",
                ["serve", "example.json", "--agent", "opencode"],
                "ExternalAgentService",
            ),
            ("worker", ["worker", "example.json"], "ExternalQueueWorker"),
            ("sender", ["sender", "example.json"], "TelegramOutboxSender"),
        )
        for label, argv, component_name in commands:
            with self.subTest(label=label):
                instances.clear()
                with (
                    patch(
                        "hermes_codex_router.cli.load_controller_config",
                        return_value=type("ControllerConfig", (), {"hub_bot": None})(),
                    ),
                    patch(
                        "hermes_codex_router.cli.load_provider_service_config",
                        return_value=type("Config", (), {"hub_bot": None})(),
                    ),
                    patch("hermes_codex_router.cli.load_hub_config", return_value=object()),
                    patch(
                        "hermes_codex_router.cli.load_external_worker_config",
                        return_value=object(),
                    ),
                    patch(
                        "hermes_codex_router.cli.load_outbox_sender_config",
                        return_value=object(),
                    ),
                    patch(f"hermes_codex_router.cli.{component_name}", Component),
                ):
                    self.assertEqual(main(argv), 0)
                self.assertEqual(len(instances), 1)
                self.assertTrue(instances[0].stopped)
                self.assertTrue(instances[0].closed)

    def test_telegram_services_use_bounded_long_poll_and_stop_before_another_poll(self) -> None:
        class State:
            def record_runtime_event(self, *_args: object) -> None:
                pass

            def get_bot_offset(self, _agent_id: str) -> None:
                return None

        class Telegram:
            def __init__(self, service: Any) -> None:
                self.service = service
                self.timeouts: list[int] = []

            def updates(self, *, offset: int | None, timeout: int) -> list[object]:
                self.timeouts.append(timeout)
                self.service.stop()
                return []

        class Agent:
            agent_id = "example-agent"
            runtime = "example-runtime"

        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller._stop = threading.Event()
        controller._queue_stop = threading.Event()
        controller._outbox_stop = threading.Event()
        controller.external_services = {}
        controller.supervisor = None
        controller.agent = Agent()
        controller.state = State()
        controller.telegram = Telegram(controller)
        controller._start_embedded_queue_consumer = lambda: None
        controller._start_controller_outbox_delivery = lambda: None
        controller.run_forever()
        self.assertEqual(controller.telegram.timeouts, [5])

        direct = cast(Any, ExternalAgentService.__new__(ExternalAgentService))
        direct._stop = threading.Event()
        direct.agent = Agent()
        direct.state = State()
        direct.telegram = Telegram(direct)
        direct.run_forever()
        self.assertEqual(direct.telegram.timeouts, [5])

    def test_failed_bounded_join_preserves_resources_owned_by_live_provider_thread(self) -> None:
        class Thread:
            def __init__(self) -> None:
                self.join_timeout: float | None = None

            def join(self, timeout: float) -> None:
                self.join_timeout = timeout

            def is_alive(self) -> bool:
                return True

        class Resource:
            def __init__(self) -> None:
                self.stopped = False
                self.closed = False

            def stop(self) -> None:
                self.stopped = True

            def close(self) -> None:
                self.closed = True

        service = cast(Any, ProjectHubService.__new__(ProjectHubService))
        service._stop = threading.Event()
        service._queue_stop = threading.Event()
        service._outbox_stop = threading.Event()
        service._queue_thread = Thread()
        service._outbox_thread = None
        provider = Resource()
        state = Resource()
        supervisor = Resource()
        service.external_services = {"example-agent": provider}
        service.state = state
        service.supervisor = supervisor
        service._discard_codex_client = lambda: None

        with self.assertRaisesRegex(ServiceError, "did not stop"):
            service.close()

        self.assertEqual(service._queue_thread.join_timeout, 5)
        self.assertTrue(provider.stopped)
        self.assertFalse(provider.closed)
        self.assertFalse(state.closed)
        self.assertFalse(supervisor.stopped)


if __name__ == "__main__":
    unittest.main()
