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
import json, sys, threading, time
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
original_thread = worker.activity.thread
def thread(**kwargs):
    result = original_thread(**kwargs)
    join = result.join
    result.join = lambda timeout=None: join(timeout=None if timeout is None else 0.01)
    return result
worker.activity.thread = thread
def background(name):
    (root / (name + "-entered")).touch()
    worker._stop.wait()
    while not (root / (name + "-release")).exists():
        time.sleep(0.01)
    (root / (name + "-finished")).touch()
def cycle():
    if all((root / (name + "-entered")).exists() for name in ("control", "connect")):
        (root / "ready").touch()
    return False
original_close = worker.state.close
def close():
    (root / "state-closing").touch()
    while not (root / "state-release").exists():
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
        deadline = time.monotonic() + 10
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail(f"child failed before {path.name}: {process.poll()}")
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
                time.sleep(0.15)
                self.assertIsNone(process.poll(), "worker abandoned its live cleanup threads")
                self.assertFalse((root / "state-closed").exists())
                (root / "control-release").touch()
                self.wait_file(root / "control-finished", process)
                time.sleep(0.05)
                self.assertIsNone(process.poll(), "worker abandoned connect cleanup")
                self.assertFalse((root / "state-closed").exists())
                process.send_signal(signal.SIGTERM)
                (root / "connect-release").touch()
                self.wait_file(root / "state-closing", process)
                process.send_signal(signal.SIGTERM)
                time.sleep(0.05)
                self.assertIsNone(process.poll(), "signal handler ended before resource close")
                (root / "state-release").touch()
                stdout, stderr = process.communicate(timeout=10)
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

    def worker(self):
        fixture = fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        worker = fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        supervisor = CodexAppServerSupervisor(
            fixture.config.codex_socket_path, manage_process=False
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
