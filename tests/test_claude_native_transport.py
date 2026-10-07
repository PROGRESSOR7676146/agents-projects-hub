"""Native route compatibility is optional, offline, explicit and fail-hard."""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.claude_native_transport_actor import (
    NATIVE_SESSION_ID,
    failure_category,
    rejected_policy_shape,
    update_terminal_shape,
)
from tests.claude_native_transport_fixture import (
    NativeTransportFixtureError,
    _copy_native_binary,
    build_native_fixture_argv,
    run_native_transport_case,
    validate_native_identity,
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
            "terminal_shape": {
                "subtype": "success",
                "is_error": False,
                "error_is_boolean": True,
                "api_error_status": None,
                "result_is_text": True,
                "assistant_error_present": False,
                "latest_assistant_error": None,
                "errors_is_list": False,
            },
            "visible_messages": 1,
            "parser_python_version": [3, 11],
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
            visible_messages=0,
            terminal_shape={
                "subtype": "success",
                "is_error": True,
                "error_is_boolean": True,
                "api_error_status": 529,
                "result_is_text": True,
                "assistant_error_present": True,
                "latest_assistant_error": "other",
                "errors_is_list": False,
            },
        )
        validate_transport_evidence(report, "bearer-reject")

    def test_529_witness_requires_exact_envelope_and_zero_visible_error_messages(self) -> None:
        report = self.report()
        report.update(
            case="bearer-reject",
            exit_code=1,
            terminal_success=False,
            terminal_failure=True,
            failure_code="claude_provider_overloaded",
            visible_messages=0,
        )
        report["terminal_shape"].update(
            is_error=True,
            api_error_status=529,
            assistant_error_present=True,
            latest_assistant_error="other",
        )
        validate_transport_evidence(report, "bearer-reject")
        for change in (
            {"failure_code": "claude_provider_failure"},
            {"visible_messages": 1},
            {"visible_messages": False},
            {"terminal_shape": {**report["terminal_shape"], "subtype": "error_during_execution"}},
            {"terminal_shape": {**report["terminal_shape"], "is_error": False}},
            {"terminal_shape": {**report["terminal_shape"], "api_error_status": None}},
            {"terminal_shape": {**report["terminal_shape"], "result_is_text": False}},
            {"terminal_shape": {**report["terminal_shape"], "assistant_error_present": False}},
        ):
            with self.subTest(change=change), self.assertRaises(NativeTransportFixtureError):
                validate_transport_evidence({**report, **change}, "bearer-reject")

    def test_fixture_inherits_every_production_text_only_argument_and_setting(self) -> None:
        from hermes_codex_router.external_runtime import ExternalCliAdapter

        with tempfile.TemporaryDirectory(prefix="example-native-argv-") as directory:
            cwd = Path(directory)
            production = list(
                ExternalCliAdapter("claude", executable="/opt/example/claude").build_argv(
                    cwd=cwd,
                    prompt="Return example-native-ok.",
                    model="claude-opus-5-5",
                    effort="high",
                    new_session_id=NATIVE_SESSION_ID,
                )
            )
            fixture = list(build_native_fixture_argv(cwd))
        settings_index = production.index("--settings") + 1
        expected_settings = json.loads(production[settings_index])
        fixture_settings = json.loads(fixture[settings_index])
        self.assertEqual(
            {key: fixture_settings[key] for key in expected_settings}, expected_settings
        )
        self.assertEqual(
            set(fixture_settings) - set(expected_settings), {"switchModelsOnFlag", "fallbackModel"}
        )
        fixture[settings_index] = production[settings_index]
        separator = production.index("--")
        self.assertEqual(fixture[:separator], production[:separator])
        self.assertEqual(fixture[-2:], production[-2:])
        self.assertEqual(
            fixture[separator:-2],
            [
                "--no-session-persistence",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--max-turns",
                "1",
                "--system-prompt",
                "Return the fixture marker.",
            ],
        )

    def test_host_refuses_unvalidated_diagnostics_before_printing_evidence(self) -> None:
        report = self.report()
        for change in (
            {"parser_python_version": [3, True]},
            {"parser_python_version": [3, 10]},
            {"parser_python_version": "example-private-runtime"},
            {"terminal_shape": {**report["terminal_shape"], "extra": "example-private-data"}},
            {"terminal_shape": {**report["terminal_shape"], "latest_assistant_error": []}},
            {"terminal_shape": {**report["terminal_shape"], "errors_is_list": 1}},
        ):
            with (
                self.subTest(change=change),
                self.assertRaises(NativeTransportFixtureError) as raised,
            ):
                validate_transport_evidence({**report, **change}, "bearer-success")
            self.assertNotIn("example-private", str(raised.exception))

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

    def test_terminal_diagnostics_handle_unicode_blanks_and_container_values(self) -> None:
        shape: dict[str, object] = {}
        update_terminal_shape("\u00a0".encode(), shape)
        self.assertEqual(shape, {})
        for value in ({"example-private": "value"}, ["example-private"], True):
            update_terminal_shape(json.dumps({"type": "assistant", "error": value}).encode(), shape)
            update_terminal_shape(json.dumps({"type": "result", "subtype": value}).encode(), shape)
            self.assertEqual(shape["latest_assistant_error"], "other")
            self.assertEqual(shape["subtype"], "other")
            self.assertNotIn("example-private", json.dumps(shape))

    def test_native_identity_mismatch_and_unbounded_version_refuse(self) -> None:
        digest, version = "a" * 64, "1.2.3 (Claude Code)"
        validate_native_identity(digest, version, digest, version)
        for observed, expected_digest, expected_version in (
            (version, "b" * 64, version),
            (version, digest, "1.2.4 (Claude Code)"),
            ("example-private-data", None, None),
        ):
            with self.subTest(observed=observed), self.assertRaises(NativeTransportFixtureError):
                validate_native_identity(digest, observed, expected_digest, expected_version)

    def test_special_file_source_refuses_before_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-native-copy-") as directory:
            source, target = Path(directory) / "fifo", Path(directory) / "copy"
            os.mkfifo(source, 0o700)
            with patch("os.open") as opening:
                with self.assertRaisesRegex(NativeTransportFixtureError, "standalone_native"):
                    _copy_native_binary(source, target)
            opening.assert_not_called()
            self.assertFalse(target.exists())

    def test_regular_binary_copy_uses_nonblocking_open_and_records_exact_digest(self) -> None:
        original_open = os.open

        def require_nonblocking(path: object, flags: int, *args: object, **kwargs: object) -> int:
            self.assertTrue(
                flags & os.O_NONBLOCK, "copy open must not block on a raced special file"
            )
            return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory(prefix="example-native-copy-") as directory:
            source, target = Path(directory) / "binary", Path(directory) / "copy"
            data = b"\x7fELFexample-binary-copy-control"
            source.write_bytes(data)
            source.chmod(0o700)
            with patch("os.open", side_effect=require_nonblocking):
                digest = _copy_native_binary(source, target)
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual(target.read_bytes(), data)

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

        for script, limit, callback, expected, diagnostic, timeout in (
            (
                "import time; time.sleep(30)",
                1024,
                None,
                NativeCaptureError,
                "native_fixture_timeout",
                0.5,
            ),
            (
                "print('x'*2048,flush=True); import time; time.sleep(30)",
                16,
                None,
                NativeCaptureError,
                "native_fixture_output_bound",
                10,
            ),
            (
                "print('x',flush=True); import time; time.sleep(30)",
                1024,
                fail,
                RuntimeError,
                "fictional callback failure",
                10,
            ),
        ):
            with self.subTest(script=script), patch("subprocess.Popen", side_effect=track):
                with self.assertRaisesRegex(expected, diagnostic) as raised:
                    capture_owned_process(
                        [sys.executable, "-I", "-c", script],
                        {"PATH": "/usr/bin:/bin"},
                        timeout=timeout,
                        stdout_limit=limit,
                        stderr_limit=1024,
                        on_stdout=callback,
                    )
                self.assertIs(type(raised.exception), expected)
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
        self.expected_sha256 = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_SHA256")
        self.expected_version = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_VERSION")
        if os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_TRANSPORT_TESTS") == "1":
            self.assertTrue(self.expected_sha256, "strict native evidence requires a binary SHA256")
            self.assertTrue(self.expected_version, "strict native evidence requires a CLI version")
            assert self.expected_sha256 is not None
            validate_native_identity(self.expected_sha256, self.expected_version, None, None)

    def run_case(self, case: str) -> None:
        report = run_native_transport_case(
            self.executable,
            case,
            expected_sha256=self.expected_sha256,
            expected_version=self.expected_version,
        )
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "case",
                        "native_binary_sha256",
                        "native_version",
                        "parser_python_version",
                        "terminal_shape",
                        "visible_messages",
                    )
                }
            ),
            flush=True,
        )

    def test_bearer_success(self) -> None:
        self.run_case("bearer-success")

    def test_bearer_http529_rejection_without_second_post(self) -> None:
        self.run_case("bearer-reject")

    def test_api_key_success(self) -> None:
        self.run_case("api-key-success")

    def test_api_key_http529_rejection_without_second_post(self) -> None:
        self.run_case("api-key-reject")
