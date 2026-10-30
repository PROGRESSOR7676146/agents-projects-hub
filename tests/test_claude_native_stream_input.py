"""Pinned native input timing and negative observation contracts; no live inference."""

from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from tests.claude_native_stream_input_fixture import (
    INPUT_IDS,
    SCENARIOS,
    _make_server,
    run_native_input,
    validate_observation,
)


class StreamInputObservationTests(unittest.TestCase):
    def report(self, scenario="tool-loop"):
        tool = scenario == "tool-loop"
        return {
            "scenario": scenario,
            "exit_code": 0,
            "posts": 2,
            "violations": 0,
            "correction_in_requests": [False, True],
            "initial_in_requests": [True, True],
            "tool_result_in_requests": [False, tool],
            "successful_read_in_requests": [False, tool],
            "stdin_open_until_results": True,
            "results": 1 if tool else 2,
            "successes": [True] if tool else [True, True],
            "echoed_ids": list(INPUT_IDS),
            "exact_echoes": True,
            "correction_before_first_result": tool,
            "same_session": True,
            "stderr_bytes": 0,
            "native_version": "2.1.296 (Claude Code)",
            "host_files_hidden": True,
            "host_loopback_blocked": True,
        }

    def test_two_distinct_native_outcomes(self):
        for scenario in SCENARIOS:
            validate_observation(self.report(scenario), scenario)

    def test_echo_or_process_success_alone_cannot_prove_consumption(self):
        changes = {
            "correction_in_requests": [False, False],
            "initial_in_requests": [True, False],
            "tool_result_in_requests": [False, False],
            "successful_read_in_requests": [False, False],
            "stdin_open_until_results": False,
            "results": 0,
            "successes": [False],
            "echoed_ids": list(reversed(INPUT_IDS)),
            "exact_echoes": False,
            "correction_before_first_result": False,
            "same_session": False,
            "posts": 3,
            "violations": 1,
            "exit_code": 1,
            "stderr_bytes": 1,
            "host_loopback_blocked": False,
            "host_files_hidden": False,
            "native_version": "2.1.297 (Claude Code)",
        }
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                report = copy.deepcopy(self.report())
                report[key] = value
                validate_observation(report, "tool-loop")

    def test_late_correction_must_not_be_reported_as_same_turn(self):
        report = self.report("text-final")
        report["correction_before_first_result"] = True
        with self.assertRaises(ValueError):
            validate_observation(report, "text-final")

    @contextmanager
    def server(self):
        release = threading.Event()
        release.set()
        counts = {"posts": 0, "violations": 0, "heads": 0}
        requests = []
        server = _make_server(
            "tool-loop", threading.Event(), release, requests, counts, threading.Lock()
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server, counts, requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_scripted_post_bound_is_conserved(self):
        with self.server() as (server, counts, requests):
            body = json.dumps(
                {"model": "claude-opus-5-5", "stream": True, "tools": [{"name": "Read"}]}
            )
            for ordinal in range(3):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                try:
                    connection.request("POST", "/v1/messages", body)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200 if ordinal < 2 else 400)
                    response.read(8192)
                finally:
                    connection.close()
            self.assertEqual(counts["posts"], 3)
            self.assertEqual(counts["violations"], 1)
            self.assertEqual(len(requests), 2)

    def test_unexpected_methods_and_parser_errors_cannot_pass_bounds(self):
        cases = [
            b"GET /v1/models HTTP/1.0\r\n\r\n",
            b"PUT /v1/messages HTTP/1.0\r\n\r\n",
            b"GET / HTTP/9.9\r\n\r\n",
            b"HEAD /unexpected HTTP/1.0\r\n\r\n",
            b"HEAD /api/hello HTTP/1.0\r\n" + b"X-Example: value\r\n" * 102 + b"\r\n",
        ]
        for request in cases:
            with self.subTest(request=request[:30]), self.server() as (server, counts, _):
                with socket.create_connection(
                    ("127.0.0.1", server.server_port), timeout=2
                ) as connection:
                    connection.sendall(request)
                    connection.shutdown(socket.SHUT_WR)
                    self.assertTrue(connection.recv(4096))
                self.assertGreater(counts["violations"], 0)
                report = self.report()
                report["violations"] = counts["violations"]
                with self.assertRaises(ValueError):
                    validate_observation(report, "tool-loop")


class NativeClaudeStreamInputTests(unittest.TestCase):
    def test_mid_tool_correction_and_terminal_boundary(self):
        executable = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_STREAM_INPUT_TESTS") == "1":
                self.fail("required offline native input executable is unavailable")
            self.skipTest("explicit offline native input executable is unavailable")
        digest = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_SHA256")
        self.assertTrue(digest, "native input evidence requires a binary SHA256")
        self.assertEqual(
            os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_VERSION"), "2.1.296 (Claude Code)"
        )
        assert executable and digest
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                report = run_native_input(Path(executable), digest, scenario)
                print("offline_native_stream_input=" + str(report))
