from __future__ import annotations

import subprocess
import unittest

from hermes_codex_router.diagnostics import _service_check


class DiagnosticsTests(unittest.TestCase):
    def test_service_check_uses_fixed_systemctl_argv(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        check = _service_check("agents-projects-hub@opencode.service", run=run)
        self.assertTrue(check.ok)
        self.assertEqual(
            calls,
            [
                (
                    "systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    "agents-projects-hub@opencode.service",
                )
            ],
        )

    def test_inactive_service_is_unhealthy(self) -> None:
        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 3, "", "")

        check = _service_check("agents-projects-hub@opencode.service", run=run)
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "inactive")


if __name__ == "__main__":
    unittest.main()
