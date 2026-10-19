"""Supervisor-owned physical socket-thread accounting, using fictional peers."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.codex_rpc import RpcError
from hermes_codex_router.codex_transports import UnixWebSocketTransport
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.hub_config import AgentDefinition
from hermes_codex_router.supervisor import CodexAppServerSupervisor
from tests import test_codex_transport_backpressure as scripted
from tests import test_external_worker as fixtures


class SocketCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="example-socket-custody-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.socket = self.root / "example.sock"
        self.socket.touch()
        self.supervisor = CodexAppServerSupervisor(self.socket, manage_process=False)
        self.supervisor.transport_mode = "socket"

    def connect(
        self, *, fail_initialize=False, stall_cleanup=False, supervisor=None, idle_probe=False
    ):
        owner = supervisor or self.supervisor
        entered, release = threading.Event(), threading.Event()
        if not stall_cleanup:
            release.set()
        self.addCleanup(release.set)
        transports = []

        class Session(scripted.ScriptedSession):
            async def __aexit__(self, *args):
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.001)

        def transport(*args, **kwargs):
            self.assertIsNotNone(kwargs.get("thread_factory"))
            result = UnixWebSocketTransport(*args, **kwargs)
            transports.append(result)
            join = result._thread.join

            def cleanup():
                release.set()
                result.close()
                join(2)
                self.assertFalse(result._thread.is_alive())

            self.addCleanup(cleanup)
            return result

        class Client:
            def __init__(self, transport, **kwargs):
                self.transport = transport

            def initialize(self, **kwargs):
                if fail_initialize:
                    raise ValueError("fictional initialization failure")

            def close(self):
                self.transport.close()

        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport", side_effect=transport),
            patch("hermes_codex_router.supervisor.CodexAppServerClient", Client),
            patch("hermes_codex_router.codex_transports.aiohttp.UnixConnector"),
            patch(
                "hermes_codex_router.codex_transports.aiohttp.ClientSession",
                return_value=Session(scripted.ScriptedSocket([])),
            ),
        ):
            if idle_probe:
                owner.transport_mode = "stdio-fallback"
                owner._socket_ineligible.set()
                original_join = threading.Thread.join
                with patch.object(
                    threading.Thread, "join", lambda thread, timeout=None: original_join(thread, 0)
                ):
                    self.assertFalse(owner.restore_socket_at_idle())
                client = None
            elif fail_initialize:
                # Preserve a real stalled transport while initialization unwinds.
                original_join = threading.Thread.join
                with patch.object(
                    threading.Thread, "join", lambda thread, timeout=None: original_join(thread, 0)
                ):
                    with self.assertRaisesRegex(ValueError, "initialization failure"):
                        owner.client(allow_fallback=False)
                client = None
            else:
                client = owner.client(allow_fallback=False, deadline=time.monotonic() + 2)
        self.assertEqual(len(transports), 1)
        return client, transports[0], entered, release

    def test_detached_unconfirmed_transport_remains_pending_until_physical_exit(self):
        _, transport, entered, release = self.connect(stall_cleanup=True)
        join = transport._thread.join
        with patch.object(transport._thread, "join", lambda timeout: join(0)):
            with self.assertRaisesRegex(RpcError, "unconfirmed"):
                transport.close()
        self.assertTrue(entered.wait(2))
        self.supervisor.retire_socket_admission()
        self.assertFalse(self.supervisor.socket_threads_drained)
        release.set()
        join(2)
        self.assertFalse(transport._thread.is_alive())
        self.assertTrue(self.supervisor.socket_threads_drained)

    def test_failed_initialization_retains_its_unconfirmed_transport(self):
        _, transport, entered, release = self.connect(fail_initialize=True, stall_cleanup=True)
        self.assertTrue(entered.wait(2))
        self.supervisor.retire_socket_admission()
        self.assertFalse(self.supervisor.socket_threads_drained)
        release.set()
        transport._thread.join(2)
        self.assertTrue(self.supervisor.socket_threads_drained)

    def test_unknown_startup_cannot_become_a_positive_exit_observation(self):
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch.object(threading.Thread, "start", side_effect=RuntimeError("fictional startup")),
        ):
            with self.assertRaisesRegex(RuntimeError, "fictional startup"):
                self.supervisor.client(allow_fallback=False)
        self.supervisor.retire_socket_admission()
        self.assertFalse(self.supervisor.socket_threads_drained)

    def test_idle_probe_uses_the_same_transport_thread_owner(self):
        _, transport, entered, release = self.connect(idle_probe=True, stall_cleanup=True)
        self.assertTrue(entered.wait(2))
        self.supervisor.retire_socket_admission()
        self.assertFalse(self.supervisor._socket_activity.drained)
        release.set()
        transport._thread.join(2)
        self.assertTrue(self.supervisor._socket_activity.drained)
        self.assertIsNone(self.supervisor.socket_threads_drained)

    def test_constructor_timeout_retains_the_thread_without_a_returned_transport(self):
        threads = []
        entered, release = threading.Event(), threading.Event()
        factory = self.supervisor._socket_activity.thread
        original_join = threading.Thread.join

        def tracked(**kwargs):
            thread = factory(**kwargs)
            threads.append(thread)
            return thread

        def cleanup():
            release.set()
            for thread in threads:
                original_join(thread, 2)
                self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)

        class ConnectingSocket(scripted.ScriptedSocket):
            async def __aenter__(self):
                await asyncio.Event().wait()
                return self

        class Session(scripted.ScriptedSession):
            async def __aexit__(self, *args):
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.001)

        with (
            patch.object(Path, "is_socket", return_value=True),
            patch.object(self.supervisor._socket_activity, "thread", tracked),
            patch.object(
                threading.Thread, "join", lambda thread, timeout=None: original_join(thread, 0)
            ),
            patch("hermes_codex_router.codex_transports.aiohttp.UnixConnector"),
            patch(
                "hermes_codex_router.codex_transports.aiohttp.ClientSession",
                return_value=Session(ConnectingSocket([])),
            ),
        ):
            with self.assertRaisesRegex(RpcError, "unconfirmed"):
                self.supervisor.client(allow_fallback=False, deadline=time.monotonic() + 0.05)
        self.assertTrue(entered.wait(2))
        self.assertEqual(len(threads), 1)
        self.supervisor.retire_socket_admission()
        self.assertFalse(self.supervisor.socket_threads_drained)
        release.set()
        threads[0].join(2)
        self.assertTrue(self.supervisor.socket_threads_drained)

    def test_retirement_refuses_every_entry_before_io_or_mode_mutation(self):
        self.supervisor.transport_mode = None
        self.supervisor.retire_socket_admission()
        with (
            patch.object(Path, "is_socket", side_effect=AssertionError("filesystem I/O")),
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as stdio,
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport") as socket,
        ):
            for call in (
                self.supervisor.start,
                self.supervisor.client,
                lambda: self.supervisor.client(allow_fallback=False),
                self.supervisor.restore_socket_at_idle,
            ):
                with self.subTest(call=call), self.assertRaisesRegex(RuntimeError, "retir"):
                    call()
            self.assertIsNone(self.supervisor.transport_mode)
            stdio.assert_not_called()
            socket.assert_not_called()
            self.assertTrue(self.supervisor.socket_threads_drained)

    def test_socket_failure_after_retirement_cannot_start_stdio_fallback(self):
        executable = self.root / "example-cli"
        executable.touch()
        owner = CodexAppServerSupervisor(
            self.socket, manage_process=False, stdio_executable=executable
        )
        owner.transport_mode = "socket"

        def fail(*args, **kwargs):
            owner.retire_socket_admission()
            raise OSError("fictional connection failure after retirement")

        with (
            patch.object(Path, "is_socket", return_value=True),
            patch("hermes_codex_router.supervisor.UnixWebSocketTransport", side_effect=fail),
            patch("hermes_codex_router.supervisor.StdioJsonLineTransport.start") as stdio,
        ):
            with self.assertRaisesRegex(OSError, "after retirement"):
                owner.client()
        stdio.assert_not_called()
        self.assertEqual(owner.transport_mode, "socket")
        self.assertTrue(owner.socket_threads_drained)

    def test_stdio_selection_permanently_excludes_socket_only_proof(self):
        executable = self.root / "example-cli"
        executable.touch()
        owner = CodexAppServerSupervisor(
            self.socket, manage_process=False, stdio_executable=executable
        )
        with patch.object(Path, "is_socket", return_value=False):
            owner.start()
        self.assertEqual(owner.transport_mode, "stdio-fallback")
        owner.transport_mode = "socket"
        owner.retire_socket_admission()
        with patch.object(Path, "is_socket", side_effect=AssertionError("getter did I/O")):
            self.assertIsNone(owner.socket_threads_drained)

    def test_managed_server_is_unsupported(self):
        owner = CodexAppServerSupervisor(self.socket)
        owner.retire_socket_admission()
        self.assertIsNone(owner.socket_threads_drained)

    def test_another_supervisor_cannot_make_this_ones_threads_disappear(self):
        other = CodexAppServerSupervisor(self.socket, manage_process=False)
        client, _, _, _ = self.connect()
        other.retire_socket_admission()
        self.assertTrue(other.socket_threads_drained)
        self.supervisor.retire_socket_admission()
        self.assertFalse(self.supervisor.socket_threads_drained)
        cast(Any, client).close()
        self.assertTrue(self.supervisor.socket_threads_drained)


class WorkerSocketRetirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExternalQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        codex = AgentDefinition(
            "codex", "Codex", "example_codex_bot", "codex", None, True, False, "example", "high"
        )
        self.config = replace(
            self.fixture.config,
            agents=(codex, *self.fixture.config.agents),
            external_worker_agent_ids=("codex", *self.fixture.config.external_worker_agent_ids),
            manage_codex_server=False,
            codex_stdio_executable=None,
        )

    def worker(self, **kwargs):
        worker = ExternalQueueWorker(self.config, "codex", registry=self.fixture.registry, **kwargs)
        self.addCleanup(worker.close)
        return worker

    def test_prestartup_drain_retires_empty_owned_socket_admission(self):
        worker = self.worker()
        worker.request_drain()
        assert worker.supervisor is not None
        with patch.object(worker.supervisor, "start", side_effect=AssertionError("startup")):
            worker.run_forever()
        self.assertTrue(worker.activity.drained)
        self.assertTrue(worker.socket_threads_drained)

    def test_injected_real_supervisor_is_never_retired_by_this_worker(self):
        supervisor = CodexAppServerSupervisor(self.config.codex_socket_path, manage_process=False)
        worker = self.worker(supervisor=supervisor)
        worker.request_drain()
        with patch.object(supervisor, "retire_socket_admission") as retire:
            worker.run_forever()
        retire.assert_not_called()
        self.assertIsNone(worker.socket_threads_drained)

    def test_replacing_an_owned_supervisor_with_a_subclass_removes_eligibility(self):
        class UnverifiedSupervisor(CodexAppServerSupervisor):
            pass

        worker = self.worker()
        supervisor = UnverifiedSupervisor(self.config.codex_socket_path, manage_process=False)
        worker.supervisor = supervisor
        worker.request_drain()
        with patch.object(supervisor, "retire_socket_admission") as retire:
            worker.run_forever()
        retire.assert_not_called()
        self.assertIsNone(worker.socket_threads_drained)

    def test_replacing_an_owned_supervisor_with_another_exact_instance_removes_eligibility(self):
        worker = self.worker()
        supervisor = CodexAppServerSupervisor(self.config.codex_socket_path, manage_process=False)
        worker.supervisor = supervisor
        self.assertIsNone(worker.socket_threads_drained)
        worker.request_drain()
        with patch.object(supervisor, "retire_socket_admission") as retire:
            worker.run_forever()
        retire.assert_not_called()
        self.assertIsNone(worker.socket_threads_drained)

    def test_socket_retirement_never_closes_an_owned_stdio_peer(self):
        worker = self.worker()
        assert worker.supervisor is not None
        worker.supervisor._socket_ineligible.set()
        worker.supervisor.transport_mode = "stdio-fallback"
        client = Mock()
        worker._codex_client = cast(Any, client)
        worker.request_drain()
        worker.run_forever()
        client.close.assert_not_called()
        self.assertIsNone(worker.socket_threads_drained)

    def test_live_operation_prevents_idle_client_retirement(self):
        worker = self.worker()
        client = Mock()
        worker._codex_client = cast(Any, client)
        with worker.activity.operation():
            worker.request_drain()
            worker._retire_idle_socket_client()
            self.assertFalse(worker.socket_threads_drained)
            client.close.assert_not_called()
        worker._retire_idle_socket_client()
        client.close.assert_called_once()
        self.assertTrue(worker.socket_threads_drained)

    def test_ordinary_close_without_retirement_does_not_create_a_proof(self):
        worker = self.worker()
        worker.close()
        self.assertFalse(worker.socket_threads_drained)

    def test_loop_retains_unconfirmed_retirement_without_repeated_close_or_cancel(self):
        worker = self.worker()
        peer = SocketCustodyTests()
        peer.setUp()
        self.addCleanup(peer.doCleanups)
        self.config.codex_socket_path.touch()
        client, transport, _, release = peer.connect(
            supervisor=worker.supervisor, stall_cleanup=True
        )
        # An exhausted ScriptedSocket waits indefinitely, rather than sending EOF.
        self.assertTrue(transport._thread.is_alive())
        self.assertFalse(transport._receiver_done.is_set())
        client = cast(Any, client)
        worker._codex_client = client
        close = Mock(wraps=client.close)
        client.close = close
        loop, task = transport._loop, transport._task
        assert loop is not None and task is not None
        original_join = transport._thread.join
        original_retire = worker._retire_idle_socket_client
        observations = []
        watchdog = threading.Timer(3, worker.stop)
        self.addCleanup(watchdog.cancel)

        def cycle():
            worker.request_drain()
            return False

        def retire():
            original_retire()
            if not worker._socket_retirement_started:
                return
            observations.append((worker.socket_threads_drained, transport._thread.is_alive()))
            if len(observations) == 3:
                release.set()
                original_join(2)
            elif len(observations) >= 4:
                worker.stop()

        with (
            patch.object(Path, "is_socket", return_value=True),
            patch.object(worker, "_run_admitted_cycle", cycle),
            patch.object(worker, "_run_connect_forever", lambda: None),
            patch.object(worker, "_retire_idle_socket_client", retire),
            patch.object(transport._thread, "join", lambda timeout: original_join(0)),
            patch.object(loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe) as schedule,
            patch("hermes_codex_router.external_worker.CodexControlMaintenance.run_forever"),
            patch("hermes_codex_router.external_worker.survived") as diagnostic,
        ):
            watchdog.start()
            worker.run_forever(poll_seconds=0.001)
        self.assertEqual(observations, [(False, True)] * 3 + [(True, False)])
        close.assert_called_once()
        self.assertEqual(sum(call.args[0] == task.cancel for call in schedule.call_args_list), 1)
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0], "external_worker.client_close")
        self.assertIsInstance(diagnostic.call_args.args[1], RpcError)
        self.assertTrue(worker.socket_threads_drained)

    def test_main_loop_retires_cached_client_once_after_operation_cleanup(self):
        worker = self.worker()
        watchdog = threading.Timer(2, worker.stop)
        self.addCleanup(watchdog.cancel)
        watchdog.start()
        assert worker.supervisor is not None
        client = Mock()
        worker._codex_client = cast(Any, client)
        events = []
        observations = []

        def cycle():
            events.append("cycle")
            worker.request_drain()
            observations.append(("cycle", worker.activity.drained, client.close.call_count))
            return False

        def close():
            assert worker.supervisor is not None
            observations.append(
                (
                    "close",
                    worker.activity.drained,
                    worker.supervisor._socket_activity.draining,
                    threading.current_thread() is threading.main_thread(),
                )
            )
            events.append("close")
            worker.stop()

        client.close.side_effect = close
        with (
            patch.object(Path, "is_socket", return_value=True),
            patch.object(worker, "_run_admitted_cycle", cycle),
            patch.object(worker, "_run_connect_forever", lambda: None),
            patch("hermes_codex_router.external_worker.CodexControlMaintenance.run_forever"),
        ):
            worker.run_forever(poll_seconds=0.001)
        self.assertEqual(events, ["cycle", "close"])
        self.assertEqual(observations, [("cycle", False, 0), ("close", True, True, True)])
        client.close.assert_called_once()
        self.assertTrue(worker.socket_threads_drained)
