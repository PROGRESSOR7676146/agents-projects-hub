from __future__ import annotations

import subprocess
import unittest

from hermes_codex_router.diagnostics import _service_check, _telegram_contract_checks
from hermes_codex_router.state import TelegramContractProvenance


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

    def test_contract_checks_are_local_optional_and_do_not_expose_provider_id(self) -> None:
        checks = _telegram_contract_checks(
            (
                TelegramContractProvenance(
                    session_id="hub-session-1",
                    agent_id="codex",
                    status="active",
                    provider_bound=True,
                    acknowledged_version=2,
                ),
                TelegramContractProvenance(
                    session_id="hub-session-2",
                    agent_id="opencode",
                    status="satellite",
                    provider_bound=False,
                    acknowledged_version=0,
                ),
            )
        )

        self.assertEqual(
            [check.name for check in checks],
            [
                "telegram_contract:hub-session-1",
                "telegram_contract:hub-session-2",
            ],
        )
        self.assertTrue(all(check.ok and not check.required for check in checks))
        self.assertEqual(
            checks[0].detail,
            "agent=codex status=active provider_bound=yes acknowledged=v2",
        )
        self.assertEqual(
            checks[1].detail,
            "agent=opencode status=satellite provider_bound=no acknowledged=v0",
        )


if __name__ == "__main__":
    unittest.main()
