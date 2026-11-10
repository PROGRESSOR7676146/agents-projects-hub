from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.external_service import ExternalAgentService
from hermes_codex_router.lifecycle import stop_on_signals
from hermes_codex_router.service import ProjectHubService, ServiceError


class Stoppable:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def stop(self) -> None:
        with self.lock:
            self.stop_calls += 1
            self.stopped.set()


class GracefulLifecycleTests(unittest.TestCase):
    def test_signal_returns_while_stop_waits_for_interrupted_thread_lock(self) -> None:
        child = r"""
import signal
import threading
from hermes_codex_router.lifecycle import stop_on_signals

lock = threading.Lock()
started = threading.Semaphore(0)
finished = threading.Semaphore(0)

class Component:
    def stop(self):
        started.release()
        with lock:
            finished.release()

class ComponentWithStopProperty:
    @property
    def stop(self):
        started.release()
        with lock:
            return finished.release

for component in (Component(), ComponentWithStopProperty()):
    with stop_on_signals(component), lock:
        signal.raise_signal(signal.SIGTERM)
        assert started.acquire(timeout=10), "stop request did not start"
        for _ in range(2):
            signal.raise_signal(signal.SIGTERM)
        assert not finished.acquire(blocking=False), "stop acquired the held lock"
    assert finished.acquire(timeout=10), "stop did not finish after the lock was released"
    assert not finished.acquire(blocking=False), "stop requests were not coalesced"
"""
        try:
            result = subprocess.run(
                [sys.executable, "-c", child], capture_output=True, text=True, timeout=25
            )
        except subprocess.TimeoutExpired:
            self.fail("signal handler deadlocked while the main thread held the stop lock")
        self.assertEqual(result.returncode, 0, result.stderr)

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
                self.assertTrue(component.stopped.wait(10), "stop request did not finish")

        self.assertEqual(component.stop_calls, 1)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous[signal.SIGINT])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous[signal.SIGTERM])

    def test_signal_requests_are_serialized_and_finish_before_context_exit(self) -> None:
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls: list[int] = []

        class BlockingStop:
            def stop(self) -> None:
                calls.append(threading.get_ident())
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("stop release was not signalled")
                finished.set()

        try:
            with stop_on_signals(BlockingStop()):
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)  # type: ignore[misc]
                self.assertTrue(entered.wait(10))
                for _ in range(5):
                    handler(signal.SIGTERM, None)  # type: ignore[misc]
                self.assertEqual(len(calls), 1)
                release.set()
            self.assertTrue(finished.is_set(), "context exited before stop finished")
            handler(signal.SIGTERM, None)  # type: ignore[misc]
            self.assertEqual(len(calls), 1, "a stop ran after context exit")
        finally:
            release.set()

    def test_failed_stop_is_reported_without_its_text_and_can_be_retried(self) -> None:
        for error_type in (RuntimeError, SystemExit):
            with self.subTest(error_type=error_type):
                failed, stopped = threading.Event(), threading.Event()

                class FailingOnce:
                    def stop(self) -> None:
                        if not failed.is_set():
                            failed.set()
                            raise error_type("fictional private stop detail")
                        stopped.set()

                with self.assertLogs("hermes_codex_router", level="WARNING") as logs:
                    with stop_on_signals(FailingOnce()):
                        handler = signal.getsignal(signal.SIGTERM)
                        assert callable(handler)
                        handler(signal.SIGTERM, None)  # type: ignore[misc]
                        self.assertTrue(failed.wait(10))
                        handler(signal.SIGTERM, None)  # type: ignore[misc]
                        self.assertTrue(stopped.wait(10))
                self.assertIn(
                    f"survived {error_type.__name__} at lifecycle.stop", "\n".join(logs.output)
                )
                self.assertNotIn("fictional private stop detail", "\n".join(logs.output))

    def test_stopper_start_failure_leaves_signal_handlers_untouched(self) -> None:
        with (
            patch("hermes_codex_router.lifecycle.threading.Thread.start", side_effect=RuntimeError),
            patch("hermes_codex_router.lifecycle.signal.signal") as install,
            self.assertRaises(RuntimeError),
        ):
            with stop_on_signals(Stoppable()):
                self.fail("context entered after thread start failed")
        install.assert_not_called()

    def test_stopper_join_timeout_is_reported_and_restores_handlers(self) -> None:
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        class BlockingStop:
            def stop(self) -> None:
                entered.set()
                release.wait(10)
                finished.set()

        previous = signal.getsignal(signal.SIGTERM)
        try:
            with (
                patch("hermes_codex_router.lifecycle._STOP_JOIN_TIMEOUT_SECONDS", 0.05),
                self.assertRaisesRegex(RuntimeError, "signal stopper did not finish"),
            ):
                with stop_on_signals(BlockingStop()):
                    handler = signal.getsignal(signal.SIGTERM)
                    assert callable(handler)
                    handler(signal.SIGTERM, None)  # type: ignore[misc]
                    self.assertTrue(entered.wait(10))
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        finally:
            release.set()
        self.assertTrue(finished.wait(10))

    def test_worker_cli_waits_for_signal_stop_before_close(self) -> None:
        entered, release, finished, joining = (threading.Event() for _ in range(4))
        instances: list[Any] = []

        class Component:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.closed_before_stop = False
                instances.append(self)

            def stop(self) -> None:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("stop release was not signalled")
                finished.set()

            def run_forever(self, **_kwargs: object) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)  # type: ignore[misc]
                assert entered.wait(10)

            def close(self) -> None:
                self.closed_before_stop = not finished.is_set()

            def shutdown_receipt(self) -> dict[str, object]:
                return {"event": "worker_shutdown_complete"}

        original_join = threading.Thread.join

        def observed_join(thread: threading.Thread, timeout: float | None = None) -> None:
            if thread.name == "signal-stopper":
                joining.set()
            original_join(thread, timeout)

        def release_when_joining() -> None:
            joining.wait(10)
            release.set()

        coordinator = threading.Thread(target=release_when_joining)
        coordinator.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = SimpleNamespace(
                    state_path=Path(directory) / "state.sqlite3",
                    worker_count_for_agent=lambda _agent_id: 1,
                )
                with (
                    patch(
                        "hermes_codex_router.cli.load_external_worker_config", return_value=config
                    ),
                    patch("hermes_codex_router.cli.ExternalQueueWorker", Component),
                    patch.object(threading.Thread, "join", observed_join),
                ):
                    self.assertEqual(main(["worker", "example.json"]), 0)
            self.assertEqual(len(instances), 1)
            self.assertFalse(instances[0].closed_before_stop, "close ran while stop was active")
        finally:
            release.set()
            coordinator.join(10)

    def test_cli_leaves_resources_open_when_signal_stopper_times_out(self) -> None:
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        instances: list[Any] = []

        class Component:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.closed = False
                instances.append(self)

            def stop(self) -> None:
                entered.set()
                release.wait(10)
                finished.set()

            def run_forever(self, **_kwargs: object) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)  # type: ignore[misc]
                assert entered.wait(10)

            def close(self) -> None:
                self.closed = True

            def shutdown_receipt(self) -> dict[str, object]:
                raise AssertionError("shutdown must not be reported as complete")

        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                state_path=Path(directory) / "state.sqlite3",
                worker_count_for_agent=lambda _agent_id: 1,
                hub_bot=None,
            )
            commands = (
                (["controller", "example.json"], "ProjectHubService"),
                (["serve", "example.json"], "ProjectHubService"),
                (["serve", "example.json", "--agent", "opencode"], "ExternalAgentService"),
                (["worker", "example.json"], "ExternalQueueWorker"),
                (["sender", "example.json"], "TelegramOutboxSender"),
                (["project-provisioner", "example.json"], "ProjectProvisioner"),
            )
            for argv, component_name in commands:
                with self.subTest(command=argv[0], component=component_name):
                    instances.clear()
                    entered.clear()
                    release.clear()
                    finished.clear()
                    try:
                        with (
                            patch("hermes_codex_router.lifecycle._STOP_JOIN_TIMEOUT_SECONDS", 0.05),
                            patch(
                                "hermes_codex_router.cli.load_controller_config",
                                return_value=config,
                            ),
                            patch(
                                "hermes_codex_router.cli.load_provider_service_config",
                                return_value=config,
                            ),
                            patch(
                                "hermes_codex_router.cli.load_external_worker_config",
                                return_value=config,
                            ),
                            patch(
                                "hermes_codex_router.cli.load_outbox_sender_config",
                                return_value=config,
                            ),
                            patch(
                                "hermes_codex_router.cli.load_project_provisioner_config",
                                return_value=config,
                            ),
                            patch(f"hermes_codex_router.cli.{component_name}", Component),
                        ):
                            self.assertEqual(main(argv), 2)
                        self.assertEqual(len(instances), 1)
                        self.assertFalse(instances[0].closed, "close ran while stop was active")
                    finally:
                        release.set()
                        self.assertTrue(finished.wait(10), "stop did not finish after release")

    def test_cli_closes_resources_when_signal_stopper_cannot_start(self) -> None:
        instances: list[Any] = []

        class Component:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.closed = False
                instances.append(self)

            def stop(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                state_path=Path(directory) / "state.sqlite3",
                worker_count_for_agent=lambda _agent_id: 1,
                hub_bot=None,
            )
            for argv, component_name in (
                (["controller", "example.json"], "ProjectHubService"),
                (["worker", "example.json"], "ExternalQueueWorker"),
            ):
                with self.subTest(command=argv[0]):
                    instances.clear()
                    with (
                        patch(
                            "hermes_codex_router.lifecycle.threading.Thread.start",
                            side_effect=RuntimeError,
                        ),
                        patch(
                            "hermes_codex_router.cli.load_controller_config", return_value=config
                        ),
                        patch(
                            "hermes_codex_router.cli.load_external_worker_config",
                            return_value=config,
                        ),
                        patch(f"hermes_codex_router.cli.{component_name}", Component),
                    ):
                        self.assertEqual(main(argv), 2)
                    self.assertEqual(len(instances), 1)
                    self.assertTrue(instances[0].closed)

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
            def __init__(self, *_args: object, **kwargs: object) -> None:
                self.kwargs = kwargs
                self.stopped = threading.Event()
                self.closed = False
                instances.append(self)

            def stop(self) -> None:
                self.stopped.set()

            def run_forever(self, **_kwargs: object) -> None:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)  # type: ignore[misc]

            def close(self) -> None:
                self.closed = True

            def shutdown_receipt(self) -> dict[str, object]:
                return {"event": "worker_shutdown_complete"}

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # The worker command reads its slot capacity and takes a private
        # per-slot process lock beside the state database before starting.
        worker_config = SimpleNamespace(
            state_path=Path(directory.name) / "state.sqlite3",
            worker_count_for_agent=lambda _agent_id: 3,
        )
        commands = (
            ("controller", ["controller", "example.json"], "ProjectHubService"),
            ("serve", ["serve", "example.json"], "ProjectHubService"),
            (
                "direct serve",
                ["serve", "example.json", "--agent", "opencode"],
                "ExternalAgentService",
            ),
            ("worker", ["worker", "example.json"], "ExternalQueueWorker"),
            ("worker slot", ["worker", "example.json", "--slot", "2"], "ExternalQueueWorker"),
            ("sender", ["sender", "example.json"], "TelegramOutboxSender"),
            ("provisioner", ["project-provisioner", "example.json"], "ProjectProvisioner"),
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
                        return_value=worker_config,
                    ),
                    patch(
                        "hermes_codex_router.cli.load_outbox_sender_config",
                        return_value=object(),
                    ),
                    patch(
                        "hermes_codex_router.cli.load_project_provisioner_config",
                        return_value=object(),
                    ),
                    patch(f"hermes_codex_router.cli.{component_name}", Component),
                ):
                    self.assertEqual(main(argv), 0)
                self.assertEqual(len(instances), 1)
                self.assertTrue(instances[0].stopped.wait(10), "stop request did not finish")
                self.assertTrue(instances[0].closed)
                if label == "worker slot":
                    self.assertEqual(
                        instances[0].kwargs, {"worker_slot": 2, "claude_subscription_tokens": {}}
                    )

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
        # Real poll-ledger lifecycle is covered by test_controller_ingress_polls.
        controller._group_ingress_polls = None
        controller.telegram = Telegram(controller)
        controller._start_embedded_queue_consumer = lambda: None
        controller._start_controller_outbox_delivery = lambda: None
        controller.run_forever()
        self.assertEqual(controller.telegram.timeouts, [5])

        direct = cast(Any, ExternalAgentService.__new__(ExternalAgentService))
        direct._stop = threading.Event()
        direct.direct_messages_only = False
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
