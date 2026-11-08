from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from hermes_codex_router import diagnostics
from hermes_codex_router.diagnostics import (
    _service_check,
    _socket_check,
    _telegram_contract_checks,
    codex_listener_state,
    run_doctor,
)
from hermes_codex_router.hub_config import HubConfig, TerminalSettings
from hermes_codex_router.migrations import LATEST_SCHEMA_VERSION
from hermes_codex_router.state import HubState, TelegramContractProvenance
from hermes_codex_router.unix_socket_diag import IncompleteEvidence, has_listener_for


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

    def test_full_profile_reports_unconfirmed_and_incompatible_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(HubState.open(root / "state.db", codex_permission_profile=None)) as state:
                topic = state.observe_topic(
                    project_id="example",
                    chat_id=1,
                    thread_id=1,
                    title="example",
                    execution_root=root,
                )
                state.activate_agent(topic.topic_id, "codex", "example-model", "low")
                check = diagnostics._codex_full_session_check(state)
                self.assertFalse(check.ok)
                self.assertIn("incompatible=1", check.detail)
                self.assertIn("/new", check.detail)

    def test_doctor_reports_local_full_as_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            HubState.open(root / "state.db", codex_permission_profile=None).close()
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(42,),
                registry_path=root / "projects.json",
                state_path=root / "state.db",
                codex_socket_path=root / "codex.sock",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(),
                agents=(),
                codex_permission_profile="full",
            )
            with patch("hermes_codex_router.diagnostics.probe_codex_config_proxy") as probe:
                probe.return_value.ok = True
                probe.return_value.detail = "example"
                report = run_doctor(config)
            checks = cast(list[dict[str, object]], report["checks"])
            check = next(item for item in checks if item["name"] == "codex_full_profile")
            self.assertIn("no Codex approvals", str(check["detail"]))
            self.assertIn("not confirmed", str(check["detail"]))
            sessions = next(item for item in checks if item["name"] == "codex_full_sessions")
            self.assertIn("incompatible=0", str(sessions["detail"]))


class CodexListenerStateTests(unittest.TestCase):
    """A leftover socket file after a crash or reboot is not a running server."""

    def test_classifies_path_without_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "codex.sock"
            self.assertEqual(codex_listener_state(path), "missing")
            os.symlink(root / "previous-boot.sock", path)
            self.assertEqual(codex_listener_state(path), "dangling")
            path.unlink()
            path.write_text("", encoding="utf-8")
            self.assertEqual(codex_listener_state(path), "not_socket")
            path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                if codex_listener_state(path) == "unverified":
                    self.skipTest("kernel Unix socket diagnostics unavailable")
                self.assertEqual(codex_listener_state(path), "no_listener")
                server.listen()
                self.assertEqual(codex_listener_state(path), "listening")
                alias = root / "alias.sock"
                os.symlink(path, alias)
                self.assertEqual(codex_listener_state(alias), "listening")
            # Closed listener: the stale file remains, exactly as after a reboot.
            self.assertEqual(codex_listener_state(path), "no_listener")
            check = _socket_check(path, required=False)
            self.assertEqual(
                (check.ok, check.detail, check.required), (False, f"no_listener: {path}", False)
            )

    def test_incomplete_or_unavailable_evidence_is_unverified_not_outage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen()
                for error in (OSError(), IncompleteEvidence("interrupted dump")):
                    with patch.object(diagnostics, "has_listener_for", side_effect=error):
                        self.assertEqual(codex_listener_state(path), "unverified")
                        self.assertTrue(_socket_check(path).ok)

    def test_listener_is_found_by_inode_through_a_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            os.symlink(real, root / "alias")
            bound = root / "alias" / "codex.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                # The kernel records the alias name, not the canonical path.
                server.bind(str(bound))
                server.listen()
                state = codex_listener_state(real / "codex.sock")
                if state == "unverified":
                    self.skipTest("kernel Unix socket diagnostics unavailable")
                self.assertEqual(state, "listening")

    def test_inode_beyond_32_bits_matches_the_kernel_truncated_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen()
                real = path.stat()
                if codex_listener_state(path) == "unverified":
                    self.skipTest("kernel Unix socket diagnostics unavailable")
                wide = os.stat_result((real.st_mode, real.st_ino + (1 << 32), *tuple(real)[2:]))
                self.assertTrue(has_listener_for(wide))

    def test_replaced_socket_during_observation_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))

                def replace_path(_: os.stat_result) -> bool:
                    path.unlink()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
                        other.bind(str(path))
                    return True

                with patch.object(diagnostics, "has_listener_for", side_effect=replace_path):
                    self.assertEqual(codex_listener_state(path), "unverified")


if __name__ == "__main__":
    unittest.main()
