"""Native route compatibility is optional, offline, explicit and fail-hard."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.claude_native_transport_actor import failure_category, rejected_policy_shape
from tests.claude_native_transport_fixture import (
    NativeTransportFixtureError,
    _copy_native_binary,
    run_native_transport_case,
    validate_transport_evidence,
)
from tests.native_process_capture import NativeCaptureError, capture_owned_process


class NativeTransportEvidenceTests(unittest.TestCase):
    def report(self) -> dict:
        return {
            "case": "bearer-success",
            "host_files_hidden": True,
            "host_loopback_blocked": True,
            "ports_distinct": True,
            "posts": 1,
            "messages_served": 1,
            "heads": 0,
            "requests": 1,
            "connections": 1,
            "timeouts": 0,
            "violations": 0,
            "exit_code": 0,
            "terminal_success": True,
            "terminal_failure": False,
            "failure_code": None,
        }

    def test_validated_success_and_terminal_rejection_are_distinct(self) -> None:
        validate_transport_evidence(self.report(), "bearer-success")
        report = self.report()
        report.update(
            case="bearer-reject",
            exit_code=1,
            terminal_success=False,
            terminal_failure=True,
            failure_code="claude_provider_overloaded",
            heads=1,
            requests=2,
        )
        validate_transport_evidence(report, "bearer-reject")

    def test_success_marker_cannot_hide_retry_unknown_request_or_server_violation(self) -> None:
        for change in (
            {"posts": 2, "requests": 2},
            {"messages_served": 0},
            {"requests": 2},
            {"violations": 1},
            {"heads": 2, "requests": 3},
            {"connections": 2},
            {"timeouts": 1},
        ):
            with self.subTest(change=change):
                report = self.report()
                report.update(change)
                with self.assertRaisesRegex(NativeTransportFixtureError, "request_surface"):
                    validate_transport_evidence(report, "bearer-success")

    def test_unknown_isolation_or_conflicting_terminal_evidence_refuses(self) -> None:
        for change in (
            {"host_loopback_blocked": False},
            {"host_files_hidden": None},
            {"exit_code": True},
            {"terminal_failure": True},
            {"terminal_success": False},
            {"case": "api-key-success"},
        ):
            with self.subTest(change=change), self.assertRaises(NativeTransportFixtureError):
                report = self.report()
                report.update(change)
                validate_transport_evidence(report, "bearer-success")

    def test_script_wrapper_refuses_without_invocation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-native-copy-") as directory:
            source, target = Path(directory) / "wrapper", Path(directory) / "copy"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o700)
            with self.assertRaisesRegex(NativeTransportFixtureError, "standalone_native"):
                _copy_native_binary(source, target)
                self.assertFalse(target.exists())

    def test_policy_diagnostics_never_retain_native_strings_or_unbounded_counts(self) -> None:
        raw = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "permissionMode": "example-private-mode",
                "plugins": [{"name": "example-private-name", "path": "example-private-path"}] * 20,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "payload": "example-private-payload",
            }
        ).encode()
        shape = rejected_policy_shape(raw)
        self.assertEqual(shape["plugins_count"], 8)
        self.assertEqual(shape["plugins"], "nonempty")
        self.assertFalse(shape["permission_matches"])
        self.assertNotIn("example-private", json.dumps(shape))
        for error in (
            NativeCaptureError("example-private-error"),
            RuntimeError("example-private-error"),
        ):
            self.assertNotIn("example-private", failure_category(error))

    def test_fifo_source_refuses_with_nonblocking_open(self) -> None:
        original_open = os.open

        def require_nonblocking(path: object, flags: int, *args: object, **kwargs: object) -> int:
            self.assertTrue(
                flags & os.O_NONBLOCK, "FIFO open must not block before file validation"
            )
            return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory(prefix="example-native-copy-") as directory:
            source, target = Path(directory) / "fifo", Path(directory) / "copy"
            os.mkfifo(source, 0o700)
            with patch("os.open", side_effect=require_nonblocking):
                with self.assertRaisesRegex(NativeTransportFixtureError, "standalone_native"):
                    _copy_native_binary(source, target)
            self.assertFalse(target.exists())

    def test_exited_leader_descendant_retaining_pipe_is_terminated_before_reap(self) -> None:
        # A duplicated read end observes EOF after cleanup. The only writer is
        # the controlled child, which holds stdout throughout its bounded sleep.
        # No PID lookup or signal to a reaped/reusable identity is needed.
        script = """import os, time
if os.fork() == 0:
    print("example-child-ready", flush=True)
    time.sleep(3)
    os._exit(0)
os._exit(0)
"""
        output = bytearray()
        pipe: int | None = None
        leader_exited: list[bool] = []
        original_popen, killpg = subprocess.Popen, os.killpg

        def track(*args: object, **kwargs: object) -> subprocess.Popen:
            nonlocal pipe
            process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
            assert process.stdout is not None
            pipe = os.dup(process.stdout.fileno())
            return process

        def terminate(group: int, sig: int) -> None:
            completion = os.waitid(os.P_PID, group, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            leader_exited.append(completion is not None and completion.si_status == 0)
            killpg(group, sig)

        try:
            with (
                patch("subprocess.Popen", side_effect=track),
                patch("os.killpg", side_effect=terminate),
            ):
                with self.assertRaisesRegex(NativeCaptureError, "native_fixture_timeout"):
                    capture_owned_process(
                        [sys.executable, "-I", "-c", script],
                        {"PATH": "/usr/bin:/bin"},
                        timeout=1,
                        stdout_limit=1024,
                        stderr_limit=1024,
                        on_stdout=output.extend,
                    )
            self.assertEqual(output, b"example-child-ready\n")
            self.assertEqual(leader_exited, [True])
            self.assertIsNotNone(pipe)
            assert pipe is not None
            monitor = select.poll()
            monitor.register(pipe, select.POLLIN | select.POLLHUP)
            events = monitor.poll(500)
            self.assertTrue(events, "owned descendant still retains stdout")
            self.assertTrue(events[0][1] & select.POLLHUP)
            self.assertEqual(os.read(pipe, 1), b"")
        finally:
            if pipe is not None:
                os.close(pipe)

    def test_timeout_output_bound_and_callback_failure_reap_owned_process(self) -> None:
        original = subprocess.Popen
        processes: list[subprocess.Popen] = []

        def track(*args: object, **kwargs: object) -> subprocess.Popen:
            process = original(*args, **kwargs)  # type: ignore[arg-type]
            processes.append(process)
            return process

        def fail(_chunk: bytes) -> None:
            raise RuntimeError("fictional callback failure")

        for script, limit, callback, expected in (
            ("import time; time.sleep(30)", 1024, None, NativeCaptureError),
            (
                "print('x'*2048,flush=True); import time; time.sleep(30)",
                16,
                None,
                NativeCaptureError,
            ),
            ("print('x',flush=True); import time; time.sleep(30)", 1024, fail, RuntimeError),
        ):
            with self.subTest(script=script), patch("subprocess.Popen", side_effect=track):
                with self.assertRaises(expected):
                    capture_owned_process(
                        [sys.executable, "-I", "-c", script],
                        {"PATH": "/usr/bin:/bin"},
                        timeout=0.5,
                        stdout_limit=limit,
                        stderr_limit=1024,
                        on_stdout=callback,
                    )
                process = processes[-1]
                self.assertIsNotNone(process.poll())
                self.assertTrue(process.stdout is not None and process.stdout.closed)
                self.assertTrue(process.stderr is not None and process.stderr.closed)


class NativeClaudeTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        executable = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE")
        if not executable:
            reason = "explicit offline native Claude executable is unavailable"
            if os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_TRANSPORT_TESTS") == "1":
                self.fail(reason)
            self.skipTest(reason)
        assert executable is not None
        self.executable = Path(executable)

    def test_bearer_success(self) -> None:
        run_native_transport_case(self.executable, "bearer-success")

    def test_bearer_http529_rejection_without_second_post(self) -> None:
        run_native_transport_case(self.executable, "bearer-reject")

    def test_api_key_success(self) -> None:
        run_native_transport_case(self.executable, "api-key-success")

    def test_api_key_http529_rejection_without_second_post(self) -> None:
        run_native_transport_case(self.executable, "api-key-reject")
