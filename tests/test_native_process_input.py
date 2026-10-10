"""Duplex fictional subprocesses: complete input is required before success."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.native_process_capture import NativeCaptureError, capture_owned_process


class NativeProcessInputTests(unittest.TestCase):
    def capture(self, script: str, data: bytes, **options: object) -> tuple[int, bytes]:
        return capture_owned_process(
            [sys.executable, "-I", "-c", script],
            {"PATH": "/usr/bin:/bin"},
            timeout=options.pop("timeout", 3),  # type: ignore[arg-type]
            stdout_limit=options.pop("stdout_limit", 262144),  # type: ignore[arg-type]
            stderr_limit=262144,
            stdin_data=data,
            **options,  # type: ignore[arg-type]
        )

    def test_input_larger_than_pipe_while_both_outputs_fill(self) -> None:
        data = b"example-input-" * 16000
        script = """import os, sys, hashlib
os.write(1, b'o'*100000)
os.write(2, b'e'*100000)
data=sys.stdin.buffer.read()
print(hashlib.sha256(data).hexdigest(),flush=True)
"""
        import hashlib

        code, output = self.capture(script, data)
        self.assertEqual(code, 0)
        self.assertEqual(output, b"o" * 100000 + hashlib.sha256(data).hexdigest().encode() + b"\n")

    def test_empty_input_closes_stdin_and_default_remains_devnull(self) -> None:
        for data in (b"", None):
            with self.subTest(data=data):
                code, output = self.capture("import sys; print(len(sys.stdin.buffer.read()))", data)  # type: ignore[arg-type]
                self.assertEqual((code, output), (0, b"0\n"))

    def test_bounds_and_type_refuse_before_spawn(self) -> None:
        for data, limit in ((b"example", 2), ("example", 100), (b"example", True), (b"x", 0)):
            with self.subTest(data=data, limit=limit), patch("subprocess.Popen") as spawn:
                with self.assertRaisesRegex(NativeCaptureError, "input_bound"):
                    self.capture("print('must not run')", data, stdin_limit=limit)  # type: ignore[arg-type]
                spawn.assert_not_called()

    def test_short_writes_and_transient_unready_write_preserve_exact_input(self) -> None:
        write = os.write
        calls = 0

        def shortened(descriptor: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError()
            if calls == 2:
                raise BlockingIOError()
            return write(descriptor, data[:17])

        data = b"fictional-stdin" * 20
        with patch("tests.native_process_capture.os.write", side_effect=shortened):
            code, output = self.capture(
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", data
            )
        self.assertEqual((code, output), (0, data))
        self.assertGreater(calls, 10)

    def test_early_closed_stdin_success_marker_cannot_hide_incomplete_input(self) -> None:
        with self.assertRaisesRegex(NativeCaptureError, "input_incomplete"):
            self.capture(
                "import os; os.close(0); print('example-success',flush=True)", b"x" * 200000
            )

    def test_timeout_callback_and_output_bound_close_all_owned_streams(self) -> None:
        original = subprocess.Popen
        processes: list[subprocess.Popen] = []

        def track(*args: object, **kwargs: object) -> subprocess.Popen:
            process = original(*args, **kwargs)  # type: ignore[arg-type]
            processes.append(process)
            return process

        def fail(_chunk: bytes) -> None:
            raise RuntimeError("fictional callback failure")

        for script, options, error in (
            ("import time; time.sleep(30)", {"timeout": 0.2}, "native_fixture_timeout"),
            (
                "print('x'*10000,flush=True); import time; time.sleep(30)",
                {"stdout_limit": 100},
                "native_fixture_output_bound",
            ),
            (
                "print('x',flush=True); import time; time.sleep(30)",
                {"on_stdout": fail},
                "fictional callback failure",
            ),
        ):
            with self.subTest(error=error), patch("subprocess.Popen", side_effect=track):
                with self.assertRaisesRegex(RuntimeError, error):
                    self.capture(script, b"x" * 200000, **options)
                process = processes[-1]
                self.assertIsNotNone(process.returncode)
                for stream in (process.stdin, process.stdout, process.stderr):
                    self.assertIsNotNone(stream)
                    assert stream is not None
                    self.assertTrue(stream.closed)
