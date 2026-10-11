"""Explicitly opted-in real CLI file permissions; no model/Telegram calls."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.claude_native_editor_fixture import run_native_editor


class NativeClaudeEditorTests(unittest.TestCase):
    def test_write_edit_and_exact_resume_require_fresh_decisions(self) -> None:
        executable = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE")
        digest = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_SHA256")
        version = os.environ.get("HUB_NATIVE_CLAUDE_FIXTURE_VERSION")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CLAUDE_EDITOR_TESTS") == "1":
                self.fail("required offline native editor executable is unavailable")
            self.skipTest("explicit offline native editor executable is unavailable")
        self.assertTrue(digest, "native editor evidence requires a binary SHA256")
        self.assertEqual(version, "2.1.285 (Claude Code)")
        assert executable is not None and digest is not None and version is not None
        report = run_native_editor(self, Path(executable), digest, version)
        self.assertEqual(report["permission_decisions"], ["allow", "deny", "allow", "deny"])
        self.assertEqual(report["distinct_requests"], 4)
        self.assertTrue(report["unchanged_before_each_decision"])
        self.assertTrue(report["exact_final_bytes"])
        self.assertTrue(report["host_files_hidden"])
        self.assertTrue(report["host_loopback_blocked"])
        self.assertEqual(report["native_tool_errors"], [False, True, False, False, True])
        self.assertEqual(report["native_successes"], 2)
        self.assertEqual(report["posts"], 7)
        self.assertEqual(report["violations"], 0)
        self.assertLessEqual(report["heads"], 2)
        self.assertTrue(report["restored_history"])
        print("offline_native_editor=" + str(report))
