from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
import time
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

    def test_prepare_reuses_a_verification_from_the_same_start(self):
        expected = service.verify_manifest(self.manifest)
        with patch.object(service, "verify_manifest") as again:
            service.verify_pins(self.manifest, expected)
        again.assert_not_called()
        binary = Path(self.manifest["codex_binary"]["path"])
        binary.chmod(0o700)
        binary.write_bytes(b"replaced binary")
        binary.chmod(0o500)
        with self.assertRaisesRegex(ValueError, "pin mismatch: codex_binary"):
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
        with (
            patch.object(service, "CUSTODY_SETTLE_SECONDS", 0.1),
            patch.object(Path, "read_bytes", side_effect=PermissionError("unavailable")),
        ):
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

        with (
            patch.object(service, "private_json", return_value=self.manifest),
            patch.object(
                service, "CURRENT_RELEASE", SimpleNamespace(clean_tree=True, git_sha="a" * 40)
            ),
            patch.object(service, "authorize_start") as authorize,
            patch.object(service.os, "execv") as execute,
            patch.dict(service.os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=False),
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
            authorize.assert_called_once_with(
                self.manifest, Path("/tmp/example-receipt"), Path("/run/user/1000")
            )
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


class BootStartTests(unittest.TestCase):
    """ADR 0088: the first start of a boot may prove closed clients itself."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        control = self.root / "home" / "app-server-control"
        control.mkdir(parents=True, mode=0o700)
        self.socket = control / "app-server-control.sock"
        self.manifest = dict(
            socket_path=str(self.socket),
            codex_binary=dict(path="/home/example/bin/codex"),
            tlive_daemon=dict(path="/home/example/tlive/daemon.mjs"),
        )

    def proc(self, *argvs: list[bytes]) -> Path:
        proc = self.root / "proc"
        proc.mkdir(exist_ok=True)
        for pid, argv in enumerate(argvs, start=10_000_000):
            entry = proc / str(pid)
            entry.mkdir()
            (entry / "cmdline").write_bytes(b"\0".join(argv))
        return proc

    def test_marker_allows_only_the_first_attempt_of_a_boot(self):
        self.assertTrue(service.claim_first_start(self.runtime, "boot-a"))
        self.assertFalse(service.claim_first_start(self.runtime, "boot-a"))
        marker = self.runtime / service.BOOT_MARKER
        self.assertEqual(marker.read_text(encoding="ascii"), "boot-a\n")
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.runtime.chmod(0o755)
        with self.assertRaises(ValueError):
            service.claim_first_start(self.runtime, "boot-a")

    def test_native_clients_and_servers_refuse_without_an_operator(self):
        service.refuse_native_clients(self.manifest, self.proc([b"/usr/bin/bash"]))
        for argv in (
            [b"node", b"/home/example/tlive/daemon.mjs"],
            [b"/home/example/bin/codex", b"resume"],
            [b"/opt/example/other-install/codex", b"resume"],
            [b"node", b"/home/example/.npm/bin/codex.js"],
            [b"node", b"/home/example/tlive/bin/tlive"],
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                with self.assertRaises(ValueError):
                    service.refuse_native_clients(self.manifest, self.proc(argv))

    def test_only_an_owned_dangling_link_from_an_earlier_boot_is_removed(self):
        now = 2_000_000_000.0
        self.assertFalse(service.remove_previous_boot_link(self.socket, now))
        self.socket.symlink_to(self.root / "cleared-at-reboot.sock")
        self.assertFalse(service.remove_previous_boot_link(self.socket, booted_at=0.0))
        self.assertTrue(service.remove_previous_boot_link(self.socket, now))
        self.assertFalse(os.path.lexists(self.socket))
        live = self.root / "live-target"
        live.write_text("", encoding="utf-8")
        self.socket.symlink_to(live)
        self.assertFalse(service.remove_previous_boot_link(self.socket, now))
        self.socket.unlink()
        self.socket.write_text("", encoding="utf-8")
        self.assertFalse(service.remove_previous_boot_link(self.socket, now))
        self.assertTrue(self.socket.exists())

    def test_without_a_receipt_only_the_first_start_proves_clients_itself(self):
        with (
            patch.object(service, "boot_identity", return_value=("boot-a", 1.0)),
            patch.object(service, "refuse_native_clients") as clients,
            patch.object(service, "refuse_existing_servers") as servers,
            patch.object(service, "require_lingering") as lingering,
            patch.object(service, "verify_manifest", return_value=self.socket) as static,
            patch.object(service, "remove_previous_boot_link") as cleanup,
            patch.object(service, "prepare") as prepare,
        ):
            service.authorize_start(self.manifest, None, self.runtime)
            lingering.assert_called_once_with()
            static.assert_called_once_with(self.manifest)
            clients.assert_called_once_with(self.manifest)
            servers.assert_called_once_with()
            cleanup.assert_called_once_with(self.socket, 1.0)
            prepare.assert_called_once_with(
                self.manifest, clients_closed=True, verified=self.socket
            )
            with self.assertRaises(ValueError):
                service.authorize_start(self.manifest, self.root / "absent-receipt", self.runtime)
            self.assertEqual(prepare.call_count, 1)
            with patch.object(service, "_receipt_valid", return_value=True):
                service.authorize_start(self.manifest, self.root / "receipt", self.runtime)
            self.assertEqual(
                (clients.call_count, cleanup.call_count, prepare.call_count), (1, 1, 2)
            )

    def test_native_codex_binary_is_found_by_executable(self):
        proc = self.proc([b"app"])
        (proc / "10000000" / "exe").symlink_to("/opt/example/codex-x86_64-unknown-linux-musl")
        with self.assertRaises(ValueError):
            service.refuse_native_clients(self.manifest, proc)

    def test_hidden_executable_of_the_user_manager_does_not_block(self):
        proc = self.proc([b"/usr/lib/systemd/systemd", b"--user"], [b"(sd-pam)"])
        with patch.object(service.os, "readlink", side_effect=PermissionError):
            service.refuse_existing_servers(proc)
            service.refuse_native_clients(self.manifest, proc)

    def test_any_other_hidden_executable_fails_closed(self):
        proc = self.proc([b"worker"])
        with (
            patch.object(service, "CUSTODY_SETTLE_SECONDS", 0.1),
            patch.object(service.os, "readlink", side_effect=PermissionError(13, "denied")),
            self.assertRaisesRegex(ValueError, r"custody unavailable: pid 10000000 \(EACCES\)"),
        ):
            service.refuse_native_clients(self.manifest, proc)

    def test_transient_denial_is_rechecked_before_classifying(self):
        # A mocked retry: one denied read, then a readable executable.
        proc = self.proc([b"(example-unit)"])
        hidden_then_codex = [PermissionError(13, "denied"), "/opt/example/codex"]
        with patch.object(service.os, "readlink", side_effect=hidden_then_codex):
            with self.assertRaisesRegex(ValueError, "client is running: pid 10000000"):
                service.refuse_native_clients(self.manifest, proc)
        hidden_then_other = [PermissionError(13, "denied"), "/usr/bin/example-agent"]
        with patch.object(service.os, "readlink", side_effect=hidden_then_other):
            service.refuse_native_clients(self.manifest, proc)

    def test_a_title_changed_during_the_recheck_still_refuses(self):
        proc = self.proc([b"placeholder"])
        cases = (
            (
                lambda: service.refuse_native_clients(self.manifest, proc),
                b"/usr/bin/python3\0/opt/example/tlive-daemon.py\0",
                "client is running: pid 10000000",
            ),
            (
                lambda: service.refuse_existing_servers(proc),
                b"/opt/example/agent\0app-server\0",
                "existing app-server requires separate ownership reconciliation: pid 10000000",
            ),
        )
        for check, first_title, refusal in cases:
            with (
                self.subTest(refusal=refusal),
                patch.object(
                    Path, "read_bytes", side_effect=[first_title, b"example-benign-worker\0"]
                ),
                patch.object(
                    service.os,
                    "readlink",
                    side_effect=[PermissionError(13, "denied"), "/usr/bin/example-worker"],
                ),
                self.assertRaisesRegex(ValueError, refusal),
            ):
                check()

    def test_one_deadline_bounds_the_whole_inventory(self):
        # Each process is denied for about 0.15 s, within a per-process window
        # of 0.2 s, so only a deadline shared by the inventory refuses.
        proc = self.proc(*([b"worker"] for _ in range(5)))
        denials: dict[str, int] = {}

        def transiently_denied(path):
            denials[str(path)] = denials.get(str(path), 0) + 1
            if denials[str(path)] <= 3:
                raise PermissionError(13, "denied")
            return "/usr/bin/example-worker"

        started = time.monotonic()
        with (
            patch.object(service, "CUSTODY_SETTLE_SECONDS", 0.2),
            patch.object(service.os, "readlink", side_effect=transiently_denied),
            self.assertRaisesRegex(ValueError, "custody unavailable"),
        ):
            service.refuse_existing_servers(proc)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_denial_refusal_carries_only_kernel_set_identity(self):
        proc = self.proc([b"worker"])
        (proc / "10000000" / "status").write_text(
            "Name:\texample\nPPid:\t42\nCapPrm:\t0000000800000000\n"
            "CapEff:\t0000000000000000\nUid:\t1000\t1000\t1000\t1000\n",
            encoding="ascii",
        )
        with (
            patch.object(service, "CUSTODY_SETTLE_SECONDS", 0.1),
            patch.object(service.os, "readlink", side_effect=PermissionError(13, "denied")),
            self.assertRaises(ValueError) as refused,
        ):
            service.refuse_native_clients(self.manifest, proc)
        self.assertEqual(
            str(refused.exception),
            "process custody unavailable: pid 10000000 (EACCES, ppid 42, CapPrm 0000000800000000)",
        )

    def test_refusal_names_the_hidden_process_and_reaches_the_journal(self):
        proc = self.proc([b"/usr/bin/example-agent"])
        with (
            patch.object(service, "CUSTODY_SETTLE_SECONDS", 0.1),
            patch.object(service.os, "readlink", side_effect=PermissionError),
            self.assertRaisesRegex(ValueError, "custody unavailable: pid 10000000"),
        ):
            service.refuse_native_clients(self.manifest, proc)
        shutil.rmtree(proc)
        native = self.proc([b"codex --token=EXAMPLE-SECRET\nforged line", b"app-server"])
        for check in (
            lambda: service.refuse_native_clients(self.manifest, native),
            lambda: service.refuse_existing_servers(native),
        ):
            with self.assertRaises(ValueError) as refused:
                check()
            self.assertTrue(str(refused.exception).endswith(": pid 10000000"))
            self.assertNotIn("EXAMPLE-SECRET", str(refused.exception))

    def test_main_reports_the_specific_refusal_reason(self):
        import contextlib
        import io

        reasons = (
            (
                ValueError("a native Codex or tlive client is running: pid 42"),
                "client is running: pid 42",
            ),
            (
                PermissionError(13, "denied", "/home/example/linger"),
                "Permission denied: /home/example/linger",
            ),
            (KeyError("hub_git_sha"), "missing manifest field hub_git_sha"),
        )
        for error, expected in reasons:
            stderr = io.StringIO()
            with (
                self.subTest(expected=expected),
                patch.object(service, "private_json", side_effect=error),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(service.main(["run", "--manifest", "/tmp/example-manifest"]), 1)
            self.assertTrue(stderr.getvalue().startswith("external Codex start refused: "))
            self.assertTrue(stderr.getvalue().endswith(f"{expected}\n"))

    def test_boot_start_requires_lingering(self):
        linger = self.root / "linger"
        linger.mkdir()
        with self.assertRaises(ValueError):
            service.require_lingering(linger)
        (linger / service.pwd.getpwuid(os.getuid()).pw_name).touch()
        service.require_lingering(linger)

    def test_cleanup_runs_only_after_static_manifest_validation(self):
        with (
            patch.object(service, "boot_identity", return_value=("boot-a", 1.0)),
            patch.object(service, "require_lingering"),
            patch.object(service, "refuse_native_clients"),
            patch.object(service, "refuse_existing_servers"),
            patch.object(service, "verify_manifest", side_effect=ValueError("pin mismatch")),
            patch.object(service, "remove_previous_boot_link") as cleanup,
            self.assertRaises(ValueError),
        ):
            service.authorize_start(self.manifest, None, self.runtime)
        cleanup.assert_not_called()

    def test_refused_first_start_still_consumes_the_boot(self):
        with (
            patch.object(service, "boot_identity", return_value=("boot-a", 1.0)),
            patch.object(service, "refuse_native_clients", side_effect=ValueError("client")),
            self.assertRaises(ValueError),
        ):
            service.authorize_start(self.manifest, None, self.runtime)
        self.assertTrue((self.runtime / service.BOOT_MARKER).exists())

    def test_boot_identity_reads_kernel_files(self):
        proc = self.root / "proc"
        (proc / "sys/kernel/random").mkdir(parents=True)
        (proc / "sys/kernel/random/boot_id").write_text("boot-a\n", encoding="ascii")
        (proc / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n", encoding="ascii")
        self.assertEqual(service.boot_identity(proc), ("boot-a", 1_700_000_000.0))

    def test_readiness_waits_for_a_kernel_listener_without_connecting(self):
        manifest = dict(socket_path=str(self.root / "s.sock"))
        with self.assertRaises(ValueError):
            service.wait_ready(manifest, timeout_seconds=0.3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(manifest["socket_path"])
            server.listen()
            with patch.object(service, "has_listener_for", return_value=True) as listener:
                service.wait_ready(manifest, timeout_seconds=1)
            listener.assert_called_once()

    def test_readiness_ends_at_once_when_the_main_process_exited(self):
        manifest = dict(socket_path=str(self.root / "s.sock"))
        proc = self.root / "proc"
        (proc / "4242").mkdir(parents=True)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "exited before it listened"):
            service.wait_ready(manifest, timeout_seconds=30, main_pid=4343, proc=proc)
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaisesRegex(ValueError, "did not become ready"):
            service.wait_ready(manifest, timeout_seconds=0.3, main_pid=4242, proc=proc)

    def test_ready_failure_is_not_reported_as_a_refused_start(self):
        import contextlib
        import io

        stderr = io.StringIO()
        with (
            patch.object(service, "private_json", return_value={}),
            patch.object(service, "wait_ready", side_effect=ValueError("not ready")),
            patch.dict(service.os.environ, {"MAINPID": "4242"}),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(service.main(["ready", "--manifest", "/tmp/example-manifest"]), 1)
        self.assertEqual(stderr.getvalue(), "external Codex readiness failed: not ready\n")

    def test_unit_start_timeout_covers_the_readiness_wait(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / "systemd/agents-projects-hub-codex-app-server.service").read_text()
        timeout = int(unit.split("TimeoutStartSec=", 1)[1].split()[0])
        self.assertGreater(timeout, service.READY_TIMEOUT_SECONDS)
        self.assertGreaterEqual(service.READY_TIMEOUT_SECONDS, 240)

    def test_unit_is_opt_in_ordered_and_waits_for_readiness(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / "systemd/agents-projects-hub-codex-app-server.service").read_text()
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn("Before=", unit)
        for consumer in (
            "agents-projects-hub.service",
            "agents-projects-hub@.service",
            "agents-projects-hub-worker@.service",
            "agents-projects-hub-codex-worker@.service",
            "tlive.service",
            "tlive.service.d/30-external-codex.conf",
        ):
            text = (root / "systemd" / consumer).read_text()
            self.assertIn("After=agents-projects-hub-codex-app-server.service", text)
            self.assertNotIn("Wants=agents-projects-hub-codex-app-server", text)
        self.assertIn("codex_server_service ready", unit)
        self.assertIn("Restart=no", unit)
