from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

from hermes_codex_router.diagnostics import (
    _service_check,
    _telegram_contract_checks,
    run_doctor,
)
from hermes_codex_router.hub_config import HubConfig, TerminalSettings
from hermes_codex_router.migrations import LATEST_SCHEMA_VERSION
from hermes_codex_router.state import HubState, TelegramContractProvenance


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_never_creates_or_migrates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=root / "projects.json",
                state_path=root / "absent" / "state.db",
                codex_socket_path=root / "codex.sock",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(),
                agents=(),
            )

            def state_check(report: dict[str, object]) -> dict[str, object]:
                checks = cast(list[dict[str, object]], report["checks"])
                return next(item for item in checks if item["name"] == "state")

            missing = state_check(run_doctor(config))
            self.assertEqual((missing["ok"], missing["detail"]), (False, "state_unavailable"))
            self.assertFalse(config.state_path.parent.exists())

            older = replace(config, state_path=root / "state.db")
            HubState.open(older.state_path, codex_permission_profile=None).close()
            with closing(sqlite3.connect(older.state_path)) as connection, connection:
                connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION - 1}")
            unsupported = state_check(run_doctor(older))
            self.assertEqual(
                (unsupported["ok"], unsupported["detail"]), (False, "state_schema_unsupported")
            )
            with closing(sqlite3.connect(older.state_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    LATEST_SCHEMA_VERSION - 1,
                )
            self.assertEqual([item.name for item in root.iterdir() if "backup" in item.name], [])

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

    def test_inaccessible_supervisor_is_not_reported_as_inactive(self) -> None:
        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, "", "bus unavailable")

        check = _service_check("agents-projects-hub@opencode.service", run=run)
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, "unavailable")

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
