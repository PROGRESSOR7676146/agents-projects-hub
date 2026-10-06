"""Optional real Codex policy rehearsal against a deterministic, offline provider."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests.codex_native_profile_fixture import (
    PROFILE_ID,
    NativeProfileFixture,
    NativeProfileFixtureError,
    proven_probe,
)


class NativeProfileEvidenceTests(unittest.TestCase):
    def probe_result(self) -> dict:
        return {
            "status": "completed",
            "thread_id": "example-thread",
            "turn_id": "example-current",
            "items": [
                {
                    "thread_id": "example-thread",
                    "turn_id": "example-current",
                    "item": {
                        "type": "commandExecution",
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "EXAMPLE_PROBE:"
                        + json.dumps(
                            {
                                "project_read": True,
                                "project_write": True,
                                "authority_read": False,
                                "authority_symlink_read": False,
                                "git_write": False,
                            }
                        ),
                    },
                }
            ],
        }

    def test_success_requires_one_completed_current_command_and_full_boolean_probe(self) -> None:
        self.assertTrue(proven_probe(self.probe_result())["project_read"])

    def test_final_completion_without_current_command_is_not_denial_evidence(self) -> None:
        result = self.probe_result()
        result["items"] = []
        with self.assertRaisesRegex(NativeProfileFixtureError, "current_execution_not_unique"):
            proven_probe(result)

    def test_old_resume_output_and_failed_commands_are_not_positive_controls(self) -> None:
        for turn_id, exit_code in (("example-old", 0), ("example-current", 1)):
            with self.subTest(turn_id=turn_id, exit_code=exit_code):
                result = self.probe_result()
                result["items"][0]["turn_id"] = turn_id
                result["items"][0]["item"]["exitCode"] = exit_code
                expected = (
                    "current_execution_not_unique"
                    if turn_id == "example-old"
                    else "current_execution_not_successful"
                )
                with self.assertRaisesRegex(NativeProfileFixtureError, expected):
                    proven_probe(result)

    def test_another_thread_duplicate_commands_and_boolean_exit_are_rejected(self) -> None:
        result = self.probe_result()
        result["items"][0]["thread_id"] = "example-other-thread"
        with self.assertRaisesRegex(NativeProfileFixtureError, "current_execution_not_unique"):
            proven_probe(result)
        result = self.probe_result()
        result["items"] *= 2
        with self.assertRaisesRegex(NativeProfileFixtureError, "current_execution_not_unique"):
            proven_probe(result)
        result = self.probe_result()
        result["items"][0]["item"]["exitCode"] = False
        with self.assertRaisesRegex(NativeProfileFixtureError, "current_execution_not_successful"):
            proven_probe(result)

    def test_truncated_partial_or_multiple_probe_payloads_are_rejected(self) -> None:
        for output, expected in (
            ('EXAMPLE_PROBE:{"project_read":true}', "probe_output_shape_invalid"),
            ("EXAMPLE_PROBE:{invalid}", "probe_output_json_invalid"),
            ("", "probe_output_not_unique"),
            (
                self.probe_result()["items"][0]["item"]["aggregatedOutput"] * 2,
                "probe_output_not_unique",
            ),
        ):
            with self.subTest(expected=expected):
                result = self.probe_result()
                result["items"][0]["item"]["aggregatedOutput"] = output
                with self.assertRaisesRegex(NativeProfileFixtureError, expected):
                    proven_probe(result)


class NativeCodexProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            reason = "explicit offline native Codex fixture executable is unavailable"
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail(reason)
            self.skipTest(reason)
        assert executable is not None
        self.fixture = self.enterContext(NativeProfileFixture(Path(executable)))

    def assertConfinedProbe(self, result: dict) -> None:
        self.assertEqual(
            proven_probe(result),
            {
                "project_read": True,
                "project_write": True,
                "authority_read": False,
                "authority_symlink_read": False,
                "git_write": False,
            },
        )

    def test_explicit_profile_survives_exact_persisted_resume_and_real_tool_execution(self) -> None:
        started = self.fixture.start_thread()
        self.assertEqual(started["activePermissionProfile"]["id"], PROFILE_ID)
        self.assertEqual(started["approvalPolicy"], "on-request")
        self.assertEqual(started["approvalsReviewer"], "user")
        thread_id = started["thread"]["id"]
        self.assertConfinedProbe(self.fixture.turn(thread_id))
        self.fixture.restart_native()
        resumed = self.fixture.resume_thread(thread_id)
        self.assertEqual(resumed["thread"]["id"], thread_id)
        self.assertEqual(resumed["activePermissionProfile"]["id"], PROFILE_ID)
        self.assertEqual(resumed["approvalPolicy"], "on-request")
        self.assertEqual(resumed["approvalsReviewer"], "user")
        self.assertConfinedProbe(self.fixture.turn(thread_id))

    def test_legacy_turn_override_loses_profile_and_does_not_prove_execution(self) -> None:
        started = self.fixture.start_thread()
        result = self.fixture.turn(started["thread"]["id"], legacy=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(
            any(setting.get("activePermissionProfile") is None for setting in result["settings"])
        )
        with self.assertRaises(NativeProfileFixtureError):
            proven_probe(result)

    def test_standalone_legacy_command_is_a_separate_policy_override(self) -> None:
        inherited = self.fixture.command_probe()
        self.assertEqual(inherited["exitCode"], 0)
        self.assertEqual(
            inherited["probe"],
            {
                "project_read": True,
                "project_write": True,
                "authority_read": False,
                "authority_symlink_read": False,
                "git_write": False,
            },
        )
        overridden = self.fixture.command_probe(legacy=True)
        self.assertEqual(overridden["exitCode"], 0)
        self.assertTrue(overridden["probe"]["project_read"])
        self.assertTrue(overridden["probe"]["project_write"])
        self.assertTrue(overridden["probe"]["authority_read"])
        self.assertTrue(overridden["probe"]["authority_symlink_read"])

    def test_profile_metadata_does_not_expose_managed_definition(self) -> None:
        profiles = self.fixture.rpc(
            "permissionProfile/list", {"cwd": str(self.fixture.project), "limit": 50}
        )["data"]
        selected = next(profile for profile in profiles if profile["id"] == PROFILE_ID)
        self.assertTrue(selected["allowed"])
        self.assertTrue(any(profile["id"] != PROFILE_ID for profile in profiles))
        self.assertTrue(
            all(not profile["allowed"] for profile in profiles if profile["id"] != PROFILE_ID)
        )
        self.assertNotIn("permissions", selected)
        requirements = self.fixture.rpc("configRequirements/read", {})["requirements"]
        self.assertEqual(requirements["defaultPermissions"], PROFILE_ID)
        self.assertEqual(requirements["allowedPermissionProfiles"], {PROFILE_ID: True})
        config = self.fixture.rpc(
            "config/read", {"cwd": str(self.fixture.project), "includeLayers": True}
        )
        self.assertIsNone(config["config"].get("permissions"))
        for layer in config["layers"]:
            self.assertIsNone(layer["config"].get("permissions"))
        self.assertNotIn("permissions", requirements)
