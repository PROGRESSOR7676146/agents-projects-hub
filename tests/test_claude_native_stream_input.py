"""Pinned native input timing and negative observation contracts; no live inference."""

from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path

from tests.claude_native_stream_input_fixture import (
    INPUT_IDS,
    SCENARIOS,
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
