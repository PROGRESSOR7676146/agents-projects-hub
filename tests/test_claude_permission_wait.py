"""Human waiting suspends only the productive budget, never stop or cleanup."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hermes_codex_router.claude_file_policy import file_tool_settings
from hermes_codex_router.claude_permission_wait import (
    HUMAN_PERMISSION_SECONDS,
    NATIVE_PERMISSION_HOOK_SECONDS,
    PERMISSION_HOOK_SECONDS,
    PermissionWaitClock,
    ProductiveDeadline,
)


class PermissionWaitTests(unittest.TestCase):
    def test_native_hook_covers_broker_and_human_response_window(self) -> None:
        self.assertEqual(HUMAN_PERMISSION_SECONDS, 7 * 86400)
        self.assertLess(HUMAN_PERMISSION_SECONDS, PERMISSION_HOOK_SECONDS)
        self.assertLess(PERMISSION_HOOK_SECONDS, NATIVE_PERMISSION_HOOK_SECONDS)
        self.assertLess(NATIVE_PERMISSION_HOOK_SECONDS * 1000, 2**31)
        for profile in ("editor", "developer"):
            settings = json.loads(file_tool_settings(Path("/usr/bin/python3"), profile=profile))
            hook = settings["hooks"]["PermissionRequest"][0]["hooks"][0]
            self.assertEqual(hook["timeout"], NATIVE_PERMISSION_HOOK_SECONDS)

    def setUp(self) -> None:
        self.now = 100.0
        self.wait = PermissionWaitClock(clock=lambda: self.now)
        self.deadline = ProductiveDeadline(900, self.wait, clock=lambda: self.now)

    def test_hours_waiting_preserve_the_remaining_productive_budget(self) -> None:
        self.now += 120
        with self.wait.waiting():
            self.now += 8 * 3600
            self.assertEqual(self.deadline.remaining(), 780)
        self.now += 779
        self.assertEqual(self.deadline.remaining(), 1)
        self.now += 2
        self.assertLess(self.deadline.remaining(), 0)

    def test_multiple_waits_count_once_and_failure_ends_waiting(self) -> None:
        with self.wait.waiting():
            self.now += 3600
            with self.assertRaises(RuntimeError):
                with self.wait.waiting():
                    pass
        with self.assertRaisesRegex(ValueError, "disconnected"):
            with self.wait.waiting():
                self.now += 7200
                raise ValueError("disconnected")
        self.now += 30
        self.assertEqual(self.wait.elapsed(), 10800)
        self.assertEqual(self.deadline.remaining(), 870)

    def test_stop_budget_cannot_be_extended_by_a_pending_permission(self) -> None:
        with self.wait.waiting():
            self.now += 3600
            self.deadline.stop_within(5)
            self.now += 6
            self.assertLess(self.deadline.remaining(), 0)

    def test_old_wait_is_not_credited_to_a_new_process(self) -> None:
        with self.wait.waiting():
            self.now += 3600
        deadline = ProductiveDeadline(10, self.wait, clock=lambda: self.now)
        self.now += 11
        self.assertLess(deadline.remaining(), 0)

    def test_unprotected_turn_keeps_its_wall_clock_budget(self) -> None:
        deadline = ProductiveDeadline(10, None, clock=lambda: self.now)
        self.now += 11
        self.assertLess(deadline.remaining(), 0)
