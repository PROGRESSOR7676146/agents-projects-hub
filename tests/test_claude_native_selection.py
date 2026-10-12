"""Host evidence refusal and optional pinned real-CLI session selection witness."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.claude_image_request_contract import MISSING_SESSION_ID
from tests.claude_native_request_contract import NATIVE_SESSION_ID, SYSTEM_PROMPT
from tests.claude_native_transport_fixture import (
    NativeTransportFixtureError,
    build_selection_fixture_argv,
    run_native_selection_case,
    validate_selection_evidence,
)
from tests.claude_selection_actor import COUNTERS
from tests.claude_selection_contract import CASES, PROMPTS, selection


def evidence(case: str) -> dict:
    base = dict.fromkeys(COUNTERS, 0)
    return {
        "case": case,
        "native_version": "2.1.285 (Claude Code)",
        "host_files_hidden": True,
        "host_loopback_blocked": True,
        "ports_distinct": True,
        "phases": [
            {
                **base,
                "phase": phase,
                "model": selection(case, phase)[0],
                "effort": selection(case, phase)[1],
                "session_id": NATIVE_SESSION_ID,
                "completed": True,
                "store_exact": True,
                "requests": 1,
                "posts": 1,
                "connections": 1,
                "validated_requests": 1,
                "messages_served": 1,
            }
            for phase in (0, 1)
        ],
        "missing": {**base, "refusal_validated": True, "store_unchanged": True, "exit_code": 1},
    }


class NativeSelectionEvidenceTests(unittest.TestCase):
    def test_all_cases_and_optional_heads_pass(self):
        for case in CASES:
            report = evidence(case)
            validate_selection_evidence(report, case)
            for entry in report["phases"]:
                entry.update(heads=1, requests=2, connections=2)
            report["missing"].update(heads=1, requests=1, connections=1)
            validate_selection_evidence(report, case)

    def test_bad_phase_evidence_unknown_fields_and_false_flags_refuse(self):
        for phase in (0, 1):
            for key, value in (
                ("posts", 0),
                ("validated_requests", 2),
                ("messages_served", False),
                ("phase", True),
                ("phase", 1 - phase),
                ("model", "example-other"),
                ("effort", "low"),
                ("session_id", MISSING_SESSION_ID),
                ("completed", False),
                ("store_exact", False),
                ("violations", 1),
                ("timeouts", 1),
                ("connections", 0),
                ("extra", "example-private"),
            ):
                report = evidence(CASES[0])
                report["phases"][phase][key] = value
                with (
                    self.subTest(phase=phase, key=key),
                    self.assertRaises(NativeTransportFixtureError),
                ):
                    validate_selection_evidence(report, CASES[0])
        for key, value in (
            ("posts", 1),
            ("validated_requests", 1),
            ("store_unchanged", False),
            ("refusal_validated", False),
            ("exit_code", True),
            ("requests", 1),
            ("extra", True),
        ):
            report = evidence(CASES[0])
            report["missing"][key] = value
            with self.subTest(key=key), self.assertRaises(NativeTransportFixtureError):
                validate_selection_evidence(report, CASES[0])
        for key, value in (
            ("host_files_hidden", False),
            ("host_loopback_blocked", None),
            ("ports_distinct", 1),
            ("native_version", "2.1.286 (Claude Code)"),
            ("phases", []),
            ("missing", None),
            ("extra", True),
        ):
            report = evidence(CASES[0])
            report[key] = value
            with self.subTest(key=key), self.assertRaises(NativeTransportFixtureError):
                validate_selection_evidence(report, CASES[0])

    def test_aggregate_two_posts_cannot_conceal_wrong_phase_distribution(self):
        report = evidence(CASES[0])
        report["phases"][0]["posts"] = 0
        report["phases"][1]["posts"] = 2
        self.assertEqual(sum(entry["posts"] for entry in report["phases"]), 2)
        with self.assertRaises(NativeTransportFixtureError):
            validate_selection_evidence(report, CASES[0])

    def test_both_start_resume_and_missing_use_full_production_builder(self):
        from hermes_codex_router.external_runtime import ExternalCliAdapter

        with tempfile.TemporaryDirectory(prefix="example-selection-argv-") as directory:
            cwd = Path(directory)
            for case in CASES:
                for phase, invocation in enumerate(build_selection_fixture_argv(cwd, case)):
                    model, effort = selection(case, min(phase, 1))
                    production = list(
                        ExternalCliAdapter("claude", executable="/opt/example/claude").build_argv(
                            cwd=cwd,
                            prompt=PROMPTS[min(phase, 1)],
                            model=model,
                            effort=effort,
                            new_session_id=NATIVE_SESSION_ID if phase == 0 else None,
                            session_id=(NATIVE_SESSION_ID if phase == 1 else MISSING_SESSION_ID)
                            if phase
                            else None,
                        )
                    )
                    fixture = list(invocation)
                    index = production.index("--settings") + 1
                    expected = json.loads(production[index])
                    actual = json.loads(fixture[index])
                    self.assertEqual(
                        actual, {**expected, "switchModelsOnFlag": False, "fallbackModel": []}
                    )
                    fixture[index] = production[index]
                    extras = [
                        "--mcp-config",
                        '{"mcpServers":{}}',
                        "--setting-sources",
                        "",
                        "--max-turns",
                        "1",
                        "--system-prompt",
                        SYSTEM_PROMPT,
                    ]
                    start = production.index("--")
                    self.assertEqual(fixture[start : start + len(extras)], extras)
                    del fixture[start : start + len(extras)]
                    self.assertEqual(fixture, production)
                    self.assertNotIn("--no-session-persistence", fixture)


class NativeClaudeSelectionTests(unittest.TestCase):
    def setUp(self):
        executable = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE")
        required = os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_SELECTION_TESTS") == "1"
        if not executable:
            if required:
                self.fail("explicit offline native executable required")
            self.skipTest("explicit offline native executable unavailable")
        self.executable = Path(executable)
        self.digest = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_SHA256")
        self.version = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_VERSION")
        self.assertTrue(self.digest, "selection witness requires pinned digest")
        self.assertTrue(self.version, "selection witness requires pinned version")

    def run_case(self, case):
        assert self.digest is not None and self.version is not None
        report = run_native_selection_case(
            self.executable, case, expected_sha256=self.digest, expected_version=self.version
        )
        print(json.dumps(report), flush=True)

    def test_opus_to_sonnet(self):
        self.run_case("opus-sonnet")

    def test_sonnet_to_opus(self):
        self.run_case("sonnet-opus")

    def test_opus_effort_only(self):
        self.run_case("opus-effort")
