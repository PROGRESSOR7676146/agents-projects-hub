from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from hermes_codex_router import codex_server_service as service
from hermes_codex_router.state import HubState


class CodexServerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        home = root / "home"
        home.mkdir(mode=0o700)
        self.state = root / "state.db"
        HubState.open(self.state, codex_permission_profile=None).close()
        binary = root / "codex"
        binary.write_bytes(b"fictional pinned binary")
        binary.chmod(0o500)
        daemon = root / "daemon"
        daemon.write_bytes(b"fictional pinned daemon")
        daemon.chmod(0o400)
        self.manifest: dict[str, Any] = dict(
            version=1,
            hub_git_sha="a" * 40,
            codex_home=str(home),
            state_path=str(self.state),
            socket_path=str(home / "app-server-control/app-server-control.sock"),
            codex_binary=dict(
                path=str(binary), sha256=hashlib.sha256(binary.read_bytes()).hexdigest()
            ),
            tlive_daemon=dict(
                path=str(daemon), sha256=hashlib.sha256(daemon.read_bytes()).hexdigest()
            ),
        )

    def test_exact_plan_and_receipt_do_not_mutate_state_or_execute(self):
        before = self.state.read_bytes()
        with patch.object(service, "refuse_existing_servers"):
            receipt = service.prepare(self.manifest, clients_closed=True, now=100)
        service.check_receipt(self.manifest, receipt, now=120)
        self.assertEqual(self.state.read_bytes(), before)
        for now in (99, 160):
            with self.assertRaises(ValueError):
                service.check_receipt(self.manifest, receipt, now=now)
        changed = dict(self.manifest, hub_git_sha="b" * 40)
        with self.assertRaises(ValueError):
            service.check_receipt(changed, receipt, now=120)
        with self.assertRaises(ValueError):
            service.prepare(self.manifest, clients_closed=False)

    def test_existing_socket_path_and_wrong_pin_refused_without_removal(self):
        path = Path(self.manifest["socket_path"])
        path.parent.mkdir(mode=0o700)
        path.write_text("unowned")
        with self.assertRaisesRegex(ValueError, "existing socket"):
            service.verify_pins(self.manifest)
        self.assertEqual(path.read_text(), "unowned")
        path.unlink()  # Test fixture only; production code has no unlink.
        self.manifest["codex_binary"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "pin mismatch"):
            service.verify_pins(self.manifest)

    def test_active_uncertain_work_and_unsettled_controls_refuse_start(self):
        from tests import test_codex_turn_controls as fixtures

        for scenario in ("executing", "unknown", "terminal", "reader", "sender", "local"):
            with self.subTest(scenario=scenario):
                f = fixtures.CodexTurnControlJournalTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                f.journal.record_turn(f.job_id, f.token, "example-turn")
                if scenario != "executing":
                    f.state.terminate_provider_job_with_notice(
                        f.job_id,
                        f.token,
                        status="indeterminate",
                        error_class="ambiguous_execution",
                        error_code="example_unknown",
                        sender_agent_id="codex",
                        telegram_html="Unknown outcome",
                        terminal_turn_status=None if scenario == "unknown" else "interrupted",
                    )
                with f.state._connection as db:
                    if scenario == "reader":
                        db.execute("""UPDATE codex_turn_controls SET read_claim_token='example-reader',
                            read_claim_owner='example-worker',read_claim_expires_at='example-expiry'""")
                    if scenario == "sender":
                        db.execute("""UPDATE codex_turn_controls SET send_started_at='example-time',
                            send_owner_token_hash='example-owner',interrupt_source='protective'""")
                    if scenario == "local":
                        db.execute("UPDATE agent_sessions SET writer_mode='local'")
                if scenario == "terminal":
                    service.require_idle_state(f.fixture.config.state_path)
                else:
                    with self.assertRaisesRegex(ValueError, "active or unknown"):
                        service.require_idle_state(f.fixture.config.state_path)

    def test_existing_process_custody_and_unreadable_inventory(self):
        root = Path(self.temp.name) / "proc"
        root.mkdir()
        p = root / "123"
        p.mkdir()
        (p / "cmdline").write_bytes(b"/opt/example/codex\0app-server\0")
        with self.assertRaisesRegex(ValueError, "existing app-server"):
            service.refuse_existing_servers(root)
        (p / "cmdline").write_bytes(b"fictional-client\0")
        service.refuse_existing_servers(root)
        with patch.object(Path, "read_bytes", side_effect=ProcessLookupError("vanished")):
            service.refuse_existing_servers(root)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("unavailable")):
            with self.assertRaisesRegex(ValueError, "custody unavailable"):
                service.refuse_existing_servers(root)

    def test_plan_rejects_dirty_or_mismatched_immutable_release_before_receipt(self):
        for clean, sha in ((False, "a" * 40), (True, "b" * 40)):
            with (
                self.subTest(clean=clean, sha=sha),
                patch.object(service, "private_json", return_value=self.manifest),
                patch.object(
                    service, "CURRENT_RELEASE", SimpleNamespace(clean_tree=clean, git_sha=sha)
                ),
                patch.object(service, "prepare", return_value={}) as prepare,
            ):
                self.assertEqual(
                    service.main(
                        [
                            "plan",
                            "--manifest",
                            "/tmp/example-manifest",
                            "--assert-native-clients-closed",
                        ]
                    ),
                    1,
                )
                prepare.assert_not_called()

    def test_wrong_schema_is_already_refused_without_migration(self):
        import sqlite3

        with sqlite3.connect(self.state) as db:
            db.execute("PRAGMA user_version=1")
        before = self.state.read_bytes()
        with self.assertRaises(service.StateError):
            service.require_idle_state(self.state)
        self.assertEqual(self.state.read_bytes(), before)

    def test_stale_passive_early_history_does_not_block_idle_start(self):
        from tests.test_preacceptance_approvals import PreacceptanceApprovalTests

        f = PreacceptanceApprovalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        scope = f.scope()
        f.observe(scope)
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            service.require_idle_state(f.root / "state.db")
        f.state.cancel_active_provider_job(f.job.job_id, f.token)
        service.require_idle_state(f.root / "state.db")

    def test_service_and_client_have_independent_force_free_custody(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / "systemd/agents-projects-hub-codex-app-server.service").read_text()
        client = (root / "systemd/tlive.service.d/30-external-codex.conf").read_text()
        for text in (unit, client):
            self.assertIn("SendSIGKILL=no", text)
            self.assertNotIn("PartOf=", text)
            self.assertNotIn("BindsTo=", text)
        self.assertIn("Restart=no", unit)
        self.assertIn("TLIVE_CODEX_APP_SERVER_MODE=attach-only", client)
        self.assertNotIn("Wants=", client)

    def test_run_uses_only_exact_pinned_argv_and_rechecks_release(self):
        from types import SimpleNamespace

        receipt = dict(
            version=1,
            manifest_sha256=service.digest(self.manifest),
            created_at=100,
            expires_at=160,
            native_clients_closed=True,
        )
        with (
            patch.object(service, "private_json", side_effect=[self.manifest, receipt]),
            patch.object(
                service, "CURRENT_RELEASE", SimpleNamespace(clean_tree=True, git_sha="a" * 40)
            ),
            patch.object(service, "check_receipt") as check,
            patch.object(service, "prepare") as prepare,
            patch.object(service.os, "execv") as execute,
            patch.dict(service.os.environ, {}, clear=False),
        ):
            self.assertEqual(
                service.main(
                    [
                        "run",
                        "--manifest",
                        "/tmp/example-manifest",
                        "--receipt",
                        "/tmp/example-receipt",
                    ]
                ),
                0,
            )
            check.assert_called_once_with(self.manifest, receipt)
            prepare.assert_called_once_with(self.manifest, clients_closed=True)
            binary = self.manifest["codex_binary"]["path"]
            execute.assert_called_once_with(binary, [binary, "app-server", "--listen", "unix://"])
        with (
            patch.object(service, "private_json", return_value=self.manifest),
            patch.object(
                service, "CURRENT_RELEASE", SimpleNamespace(clean_tree=False, git_sha="a" * 40)
            ),
            patch.object(service.os, "execv") as execute,
        ):
            self.assertEqual(
                service.main(
                    [
                        "run",
                        "--manifest",
                        "/tmp/example-manifest",
                        "--receipt",
                        "/tmp/example-receipt",
                    ]
                ),
                1,
            )
            execute.assert_not_called()
