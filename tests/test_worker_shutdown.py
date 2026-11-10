"""Physical cleanup regressions; no live providers, services or deployment state."""

from __future__ import annotations

import contextlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.codex_rpc import RpcError
from hermes_codex_router.supervisor import CodexAppServerSupervisor
from tests import test_external_worker as fixtures

CHILD = r"""
import json, signal, sys, threading, time
from pathlib import Path
from unittest.mock import patch
from hermes_codex_router.cli import main
from hermes_codex_router.supervisor import CodexAppServerSupervisor
from tests.test_external_worker import ExternalQueueWorkerTests, Adapter

root = Path(sys.argv[1])
fixture = ExternalQueueWorkerTests()
fixture.setUp()
worker = fixture.worker("opencode", Adapter("opencode"))
from dataclasses import replace
worker.agent = replace(worker.agent, runtime="codex", agent_id="codex")
supervisor = CodexAppServerSupervisor(root / "example.sock", manage_process=False)
worker.supervisor = worker._owned_supervisor = supervisor
supervisor.start = lambda: None
original_stop = worker.stop
stop_calls = 0
stop_lock = threading.Lock()
def stop():
    global stop_calls
    original_stop()
    with stop_lock:
        stop_calls += 1
        (root / ("stop-" + str(stop_calls))).touch()
worker.stop = stop
original_thread = worker.activity.thread
def thread(**kwargs):
    result = original_thread(**kwargs)
    join = result.join
    def observed_join(timeout=None):
        if timeout is None:
            name = kwargs["name"].rsplit("-", 1)[-1]
            (root / (name + "-joining")).touch()
        return join(timeout=None if timeout is None else 0.01)
    result.join = observed_join
    return result
worker.activity.thread = thread
received = []
def background(name):
    (root / (name + "-entered")).touch()
    worker._stop.wait()
    while not (root / (name + "-release")).exists():
        time.sleep(0.01)
    (root / (name + "-finished")).touch()
def cycle():
    if not (root / "ready").exists() and all(
        (root / (name + "-entered")).exists() for name in ("control", "connect")
    ):
        original_handler = signal.getsignal(signal.SIGTERM)
        assert callable(original_handler)
        def observe_signal(signum, frame):
            received.append(signum)
            original_handler(signum, frame)
        signal.signal(signal.SIGTERM, observe_signal)
        (root / "ready").touch()
    return False
original_close = worker.state.close
def close():
    (root / "state-closing").touch()
    while not (root / "state-release").exists():
        for number in range(1, len(received) + 1):
            (root / ("signal-" + str(number))).touch()
        time.sleep(0.01)
    (root / "state-closed").touch()
    original_close()
worker.state.close = close
worker._run_connect_forever = lambda: background("connect")
worker.run_cycle = cycle
try:
    with patch("hermes_codex_router.cli.load_external_worker_config", return_value=worker.config), \
         patch("hermes_codex_router.cli.ExternalQueueWorker", return_value=worker), \
         patch("hermes_codex_router.external_worker.CodexControlMaintenance.run_forever", lambda _: background("control")):
        result = main(["worker", "example.json", "--agent", "opencode", "--poll-seconds", "0.01"])
    (root / "returned").write_text(str(result))
finally:
    fixture.tearDown()
"""


class WorkerShutdownTests(unittest.TestCase):
    def wait_file(self, path: Path, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 30
        while not path.exists():
            returncode = process.poll()
            if returncode is not None:
                detail = process.stderr.read() if process.stderr is not None else ""
                self.fail(f"child exited before {path.name}: {returncode}\n{detail}")
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for {path.name}; child is still running")
            time.sleep(0.01)

    def test_real_sigterm_keeps_state_and_process_until_both_background_threads_exit(self):
        with tempfile.TemporaryDirectory(prefix="example-worker-exit-") as temporary:
            root = Path(temporary)
            process = subprocess.Popen(
                [sys.executable, "-c", CHILD, str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.wait_file(root / "ready", process)
                process.send_signal(signal.SIGTERM)
                self.wait_file(root / "stop-1", process)
                self.wait_file(root / "control-joining", process)
                self.assertIsNone(process.poll(), "worker abandoned its live cleanup threads")
                self.assertFalse((root / "control-finished").exists())
                self.assertFalse((root / "connect-finished").exists())
                self.assertFalse((root / "state-closing").exists())
                self.assertFalse((root / "state-closed").exists())
                (root / "control-release").touch()
                self.wait_file(root / "control-finished", process)
                self.wait_file(root / "connect-joining", process)
                self.assertIsNone(process.poll(), "worker abandoned connect cleanup")
                self.assertFalse((root / "connect-finished").exists())
                self.assertFalse((root / "state-closing").exists())
                self.assertFalse((root / "state-closed").exists())
                process.send_signal(signal.SIGTERM)
                (root / "connect-release").touch()
                self.wait_file(root / "state-closing", process)
                self.wait_file(root / "stop-2", process)  # close() also requests stop
                self.wait_file(root / "signal-2", process)
                self.assertTrue((root / "connect-finished").exists())
                process.send_signal(signal.SIGTERM)
                self.wait_file(root / "signal-3", process)
                self.assertIsNone(process.poll(), "signal handler ended before resource close")
                self.assertFalse((root / "state-closed").exists())
                self.assertFalse((root / "stop-3").exists(), "repeated signals were not coalesced")
                (root / "state-release").touch()
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue((root / "state-closed").exists())
                self.assertTrue((root / "returned").exists())
                self.assertIn('"event": "worker_shutdown_complete"', stdout)
            finally:
                for name in ("control", "connect"):
                    (root / (name + "-release")).touch()
                (root / "state-release").touch()
                if process.poll() is None:
                    process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    def worker(self, *, manage_process=False):
        fixture = fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        worker = fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        supervisor = CodexAppServerSupervisor(
            fixture.config.codex_socket_path, manage_process=manage_process
        )
        worker.supervisor = worker._owned_supervisor = supervisor
        return worker, supervisor

    def test_failed_close_waits_for_discarded_socket_threads_and_preserves_state(self):
        worker, supervisor = self.worker()
        entered, release, close_requested = threading.Event(), threading.Event(), threading.Event()
        failures: list[Exception] = []

        def socket_thread():
            entered.set()
            release.wait()

        class Client:
            def close(self):
                close_requested.set()
                raise RpcError("fictional unconfirmed close")

        client = Client()
        worker._codex_client = client  # type: ignore[assignment]
        socket_owner = supervisor._socket_activity.thread(target=socket_thread)
        socket_owner.start()

        def close():
            try:
                worker.close()
            except Exception as error:
                failures.append(error)

        closer = threading.Thread(target=close)
        try:
            self.assertTrue(entered.wait(2))
            closer.start()
            self.assertTrue(close_requested.wait(2))
            closer.join(0.1)
            self.assertTrue(closer.is_alive(), "unconfirmed socket cleanup was abandoned")
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                worker.shutdown_receipt()
            self.assertFalse(worker._closed)
            self.assertIs(worker._codex_client, client)
        finally:
            release.set()
            socket_owner.join(2)
            closer.join(2)
            worker._codex_client = None
        self.assertFalse(socket_owner.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RpcError)
        worker.state.status_snapshot()  # The original SQLite connection is still usable.
        worker.close()
        self.assertTrue(worker.shutdown_receipt()["socket_threads_drained"])

    def test_failed_close_stops_owned_peer_before_waiting_for_socket_threads(self):
        for pending_socket in (False, True):
            with self.subTest(pending_socket=pending_socket):
                worker, supervisor = self.worker(manage_process=True)
                supervisor._acquire_socket_ownership()
                peer = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import socket, sys, time; "
                        "s = socket.socket(socket.AF_UNIX); "
                        "s.bind(sys.argv[1]); s.listen(); time.sleep(60)",
                        str(supervisor.socket_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                supervisor.process = peer  # type: ignore[assignment]
                supervisor.transport_mode = "managed-socket"
                close_requested = threading.Event()
                failures: list[Exception] = []

                class Client:
                    def close(self):
                        close_requested.set()
                        raise RpcError("fictional managed close failure")

                client = Client()
                worker._codex_client = client  # type: ignore[assignment]
                socket_owner = supervisor._socket_activity.thread(target=peer.wait)

                def close():
                    try:
                        worker.close()
                    except Exception as error:
                        failures.append(error)

                closer = threading.Thread(target=close)
                try:
                    self.wait_file(supervisor.socket_path, peer)
                    if pending_socket:
                        socket_owner.start()
                    closer.start()
                    self.assertTrue(close_requested.wait(2))
                    closer.join(2)
                    self.assertFalse(closer.is_alive(), "peer exit must precede socket wait")
                    self.assertIsNotNone(peer.poll(), "owned peer survived failed client close")
                    self.assertIsNone(supervisor.process)
                    self.assertIsNone(supervisor._ownership_file)
                    self.assertFalse(supervisor.socket_path.exists())
                    self.assertTrue(supervisor._socket_activity.drained)
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], RpcError)
                    self.assertIs(worker._codex_client, client)
                    self.assertFalse(worker._closed)
                    with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                        worker.shutdown_receipt()
                    worker.state.status_snapshot()
                finally:
                    supervisor.stop()
                    if pending_socket and socket_owner.ident is not None:
                        socket_owner.join(2)
                    if closer.ident is not None:
                        closer.join(2)
                    worker._codex_client = None
                worker.close()
                self.assertIsNone(worker.shutdown_receipt()["socket_threads_drained"])

    def test_peer_stop_failure_preserves_state_and_denies_success_receipt(self):
        worker, supervisor = self.worker(manage_process=True)
        with patch.object(supervisor, "stop", side_effect=OSError("fictional peer stop fault")):
            with self.assertRaisesRegex(OSError, "fictional peer stop fault"):
                worker.close()
        self.assertTrue(supervisor._socket_activity.drained)
        self.assertFalse(worker._closed)
        worker.state.status_snapshot()
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            worker.shutdown_receipt()

    def test_failed_close_does_not_stop_an_injected_supervisor(self):
        worker, supervisor = self.worker()
        worker._owned_supervisor = None
        with (
            patch.object(worker, "_discard_client", side_effect=RpcError("fictional close fault")),
            patch.object(supervisor, "stop") as stop,
        ):
            with self.assertRaisesRegex(RpcError, "fictional close fault"):
                worker.close()
        stop.assert_not_called()
        self.assertFalse(worker._closed)
        worker.state.status_snapshot()

    def test_stdio_is_never_reported_as_socket_cleanup_proof(self):
        worker, supervisor = self.worker()
        supervisor._socket_ineligible.set()
        supervisor.transport_mode = "stdio-fallback"
        worker.close()
        self.assertIsNone(worker.shutdown_receipt()["socket_threads_drained"])

    def test_prior_stdio_ineligibility_does_not_abandon_later_socket_threads(self):
        _, supervisor = self.worker()
        supervisor._socket_ineligible.set()
        entered, release = threading.Event(), threading.Event()

        def socket_thread():
            entered.set()
            release.wait()

        socket_owner = supervisor._socket_activity.thread(target=socket_thread)
        waiter = threading.Thread(target=supervisor.wait_for_socket_threads)
        socket_owner.start()
        try:
            self.assertTrue(entered.wait(2))
            waiter.start()
            waiter.join(0.1)
            self.assertTrue(waiter.is_alive())
            self.assertIsNone(supervisor.socket_threads_drained)
        finally:
            release.set()
            socket_owner.join(2)
            waiter.join(2)
        self.assertFalse(socket_owner.is_alive())
        self.assertFalse(waiter.is_alive())

    def test_state_close_failure_cannot_produce_a_success_receipt(self):
        worker, _ = self.worker()
        with patch.object(worker.state, "close", side_effect=RuntimeError("fictional close fault")):
            with self.assertRaisesRegex(RuntimeError, "fictional"):
                worker.close()
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            worker.shutdown_receipt()

    def test_cli_close_failure_is_safe_and_has_no_success_receipt(self):
        worker, _ = self.worker()
        output = io.StringIO()
        with (
            patch(
                "hermes_codex_router.cli.load_external_worker_config", return_value=worker.config
            ),
            patch("hermes_codex_router.cli.ExternalQueueWorker", return_value=worker),
            patch.object(worker, "run_forever"),
            patch.object(worker.state, "close", side_effect=OSError("fictional private payload")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["worker", "example.json", "--agent", "opencode"]), 2)
        self.assertEqual(
            json.loads(output.getvalue()), {"ok": False, "error": "worker cleanup is unconfirmed"}
        )
        self.assertNotIn("fictional private payload", output.getvalue())


if __name__ == "__main__":
    unittest.main()
