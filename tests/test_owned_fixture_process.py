"""Lifecycle tests launch fictional Python peers only, never providers."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests.native_process_capture import owned_fixture_process


class OwnedFixtureProcessTests(unittest.TestCase):
    def test_exception_kills_reaps_and_closes_every_owned_pipe(self) -> None:
        process: subprocess.Popen[bytes] | None = None
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "example-fixture-failure"):
            with owned_fixture_process(
                [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                {},
                stdin=subprocess.PIPE,
            ) as process:
                self.assertIsNotNone(process.stdin)
                self.assertIsNotNone(process.stdout)
                self.assertIsNotNone(process.stderr)
                raise RuntimeError("example-fixture-failure")
        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 10)
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is not None
            self.assertTrue(stream.closed)

    def test_exited_leader_is_not_reaped_before_descendant_group_cleanup(self) -> None:
        source = (
            "import os,signal,time\n"
            "if os.fork()==0:\n"
            " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            " os.write(1,b'example-ready')\n"
            " time.sleep(60)\n"
            "else: os._exit(0)\n"
        )
        original_killpg = os.killpg
        observed: list[bool] = []
        duplicate = -1
        process: subprocess.Popen[bytes] | None = None

        def kill(group: int, sig: int) -> None:
            assert process is not None
            self.assertIsNone(process.returncode)
            self.assertIsNotNone(os.waitid(os.P_PID, group, os.WEXITED | os.WNOHANG | os.WNOWAIT))
            observed.append(True)
            original_killpg(group, sig)

        try:
            with patch("os.killpg", side_effect=kill):
                with owned_fixture_process([sys.executable, "-I", "-c", source], {}) as process:
                    assert process.stdout is not None
                    duplicate = os.dup(process.stdout.fileno())
                    with selectors.DefaultSelector() as selector:
                        selector.register(duplicate, selectors.EVENT_READ)
                        self.assertTrue(selector.select(5))
                        self.assertEqual(os.read(duplicate, 64), b"example-ready")
                    deadline = time.monotonic() + 5
                    while (
                        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        is None
                    ):
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
            self.assertEqual(observed, [True])
            assert process is not None
            self.assertEqual(process.returncode, 0)
            with selectors.DefaultSelector() as selector:
                selector.register(duplicate, selectors.EVENT_READ)
                self.assertTrue(selector.select(5))
                self.assertEqual(os.read(duplicate, 64), b"")
        finally:
            if duplicate >= 0:
                os.close(duplicate)
