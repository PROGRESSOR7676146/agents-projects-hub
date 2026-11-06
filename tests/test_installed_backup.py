from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_deployment_manifest import _wheel

from hermes_codex_router.cli import main
from hermes_codex_router.deployment_manifest import create_deployment_manifest
from hermes_codex_router.installed_backup import (
    InstalledBackupError,
    backup_installation,
    describe_installation,
    file_descriptor,
    inspect_backup,
)
from hermes_codex_router.runtime_release import installed_runtime_digest


class InstalledBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "hub.json"
        self.database = self.root / "state.db"
        self.registry = self.root / "projects.json"
        self.registry.write_text('{"schema_version":1,"projects":[]}\n')
        self.registry.chmod(0o600)
        self.configuration = {
            "schema_version": 1,
            "owner_user_ids": [123456789],
            "registry_path": str(self.registry),
            "state_path": str(self.database),
            "codex_socket_path": str(self.root / "codex.sock"),
            "dispatch_mode": "queue",
            "queue_runtime": "external",
            "outbox_runtime": "external",
            "external_worker_agent_ids": ["codex"],
            "agents": [
                {
                    "agent_id": "codex",
                    "display_name": "Codex",
                    "runtime": "codex",
                    "telegram_username": "example_codex_bot",
                    "token_file": str(self.root / "not-read.token"),
                    "default_model": "example-model",
                    "default_effort": "high",
                }
            ],
            "projects": [{"project_id": "example"}],
        }
        self.write_config()
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute("PRAGMA user_version=54")
            con.execute("CREATE TABLE saved_work (id TEXT PRIMARY KEY, result TEXT)")
            con.execute("INSERT INTO saved_work VALUES ('uncertain', 'original')")
        self.database.chmod(0o600)
        self.preupgrade = self.root / "preupgrade.db"
        with (
            closing(sqlite3.connect(self.database)) as source,
            closing(sqlite3.connect(self.preupgrade)) as target,
        ):
            source.backup(target)
        self.preupgrade.chmod(0o600)
        self.rollback = _wheel(
            self.root / "old.whl", version="0.6.0", git_sha="b" * 40, schema_max=56
        )
        self.current = _wheel(
            self.root / "new.whl", version="0.7.0", git_sha="a" * 40, schema_max=56
        )
        self.manifest = self.root / "release.json"
        create_deployment_manifest(
            self.manifest,
            active_artifact=self.current,
            rollback_artifact=self.rollback,
            configuration=self.config,
            state_backup=self.preupgrade,
        )
        self.packages = {}
        for label, wheel in (("active", self.current), ("rollback", self.rollback)):
            site = self.root / "releases" / label / "venv/lib/python3.11/site-packages"
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(site)
            self.packages[label] = site / "hermes_codex_router"
            runtime = self.root / "releases" / label
            executable = runtime / "venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fictional fixture interpreter; never executed\n")
            (runtime / "venv/pyvenv.cfg").write_text(
                "version = 3.11.0\ninclude-system-site-packages = false\n"
            )
            for p in sorted(runtime.rglob("*"), reverse=True):
                p.chmod(0o500 if p.is_dir() or p == executable else 0o400)
            runtime.chmod(0o500)
        self.units: dict[str, dict] = {}
        self.spec = self.root / "installed.json"
        self.document = {
            "kind": "hub-installed-deployment",
            "schema_version": 1,
            "configuration": file_descriptor(self.config),
            "components": [],
            "auxiliary": [],
            "recovery_artifacts": [],
        }
        for component, instance in (
            ("controller", "project-hub-controller"),
            ("monitor", "operations-monitor"),
            ("sender", "telegram-outbox-sender"),
            ("provider_worker", "codex-worker"),
        ):
            self.add_component(component, instance)
        self.write_spec()

    def tearDown(self) -> None:
        for p in self.root.rglob("*"):
            if p.is_dir():
                p.chmod(0o700)
        self.temp.cleanup()

    def write_config(self) -> None:
        self.config.write_text(json.dumps(self.configuration))
        self.config.chmod(0o600)

    def add_component(self, component: str, instance: str, release: str = "active") -> None:
        unit = f"agents-projects-hub-{instance}.service"
        runtime = self.packages[release].parents[4]
        executable = runtime / "venv/bin/python"
        command = {"provider_worker": "worker", "project_provisioner": "project-provisioner"}.get(
            component, component
        )
        argv = [
            str(executable),
            "-I",
            "-B",
            "-m",
            "hermes_codex_router.cli",
            command,
            str(self.config),
        ]
        if component == "provider_worker":
            argv += [
                "--agent",
                "codex",
                "--slot",
                "1" if instance == "codex-worker" else instance.rsplit("-", 1)[-1],
            ]
        self.document["components"].append(
            {
                "component": component,
                "instance_id": instance,
                "unit": unit,
                "configuration": file_descriptor(self.config),
                "release_manifest": file_descriptor(self.manifest),
                "artifact": release,
                "package_root": str(self.packages[release]),
                "argv": argv,
                "executable": file_descriptor(executable),
                "runtime": {"root": str(runtime), "sha256": installed_runtime_digest(runtime)},
            }
        )
        self.units[unit] = {
            "argv": argv,
            "executable": str(executable),
            "start_marker": "fixture-incarnation-1",
        }

    def write_spec(self) -> None:
        self.spec.write_text(json.dumps(self.document))
        self.spec.chmod(0o600)

    def observe(self, units: tuple[str, ...]) -> dict:
        return self.units

    def add_runtime_file(self, relative: str, text: str) -> None:
        runtime = self.packages["active"].parents[4]
        for p in (runtime, *runtime.rglob("*")):
            if p.is_dir():
                p.chmod(0o700)
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.chmod(0o600)
        target.write_text(text)
        for p in (*sorted(runtime.rglob("*"), reverse=True), runtime):
            p.chmod(p.stat().st_mode & ~0o222)
        digest = installed_runtime_digest(runtime)
        for record in self.document["components"]:
            if record["runtime"]["root"] == str(runtime):
                record["runtime"]["sha256"] = digest
        self.write_spec()

    def test_duplicate_python_import_sites_refuse_even_with_matching_pin(self) -> None:
        self.add_runtime_file(
            "venv/lib/python3.12/site-packages/hermes_codex_router/__init__.py", ""
        )
        with self.assertRaisesRegex(InstalledBackupError, "one version-matched site"):
            describe_installation(self.spec, observe=self.observe)

    def test_python_version_must_match_the_declared_import_site(self) -> None:
        self.add_runtime_file(
            "venv/pyvenv.cfg", "version = 3.12.0\ninclude-system-site-packages = false\n"
        )
        with self.assertRaisesRegex(InstalledBackupError, "one version-matched site"):
            describe_installation(self.spec, observe=self.observe)

    def test_import_redirect_refuses_even_with_matching_runtime_pin(self) -> None:
        self.add_runtime_file("venv/lib/python3.11/site-packages/redirect.pth", "import sys\n")
        with self.assertRaisesRegex(InstalledBackupError, "unsupported redirects"):
            describe_installation(self.spec, observe=self.observe)

    def test_unverified_bytecode_cannot_override_wheel_sources(self) -> None:
        self.add_runtime_file(
            "venv/lib/python3.11/site-packages/hermes_codex_router/__pycache__/module.pyc",
            "fixture",
        )
        with self.assertRaisesRegex(InstalledBackupError, "unverified bytecode"):
            describe_installation(self.spec, observe=self.observe)

    def test_versioned_description_checks_artifacts_and_derives_topology(self) -> None:
        result = describe_installation(self.spec, observe=self.observe)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["databases"]["shared"]["schema_version"], 54)
        self.assertEqual(len(result["components"]), 4)
        self.assertFalse(result["runtime_acceptance"])
        self.assertFalse((self.root / "not-read.token").exists())

    def test_compatible_mixed_components_do_not_require_restart(self) -> None:
        self.document["components"].pop()
        self.add_component("provider_worker", "codex-worker", "rollback")
        self.write_spec()
        result = describe_installation(self.spec, observe=self.observe)
        self.assertEqual(result["revision_status"], "mixed")
        self.assertTrue(result["backup_compatible"])

    def test_unknown_consumer_and_changed_process_refuse_before_snapshot(self) -> None:
        self.units["agents-projects-hub-unknown.service"] = {}
        with self.assertRaisesRegex(InstalledBackupError, "unknown.*consumer"):
            backup_installation(self.spec, self.root / "snapshot", observe=self.observe)
        self.assertFalse((self.root / "snapshot").exists())
        self.units.pop("agents-projects-hub-unknown.service")
        self.units[next(iter(self.units))]["argv"] = ["unexpected"]
        with self.assertRaisesRegex(InstalledBackupError, "process identity"):
            describe_installation(self.spec, observe=self.observe)

    def test_changed_installed_bytes_and_config_are_not_trusted(self) -> None:
        module = self.packages["active"] / "_build_info.py"
        module.chmod(0o600)
        module.write_text("CLEAN_TREE=True\n")
        module.chmod(0o400)
        with self.assertRaisesRegex(
            (InstalledBackupError, ValueError), "digest|installed.*artifact"
        ):
            describe_installation(self.spec, observe=self.observe)

    def test_snapshot_preserves_work_and_old_pending_and_never_replays(self) -> None:
        unrelated = self.root / "PENDING.json"
        unrelated.write_bytes(b"external original pending")
        output = self.root / "snapshot"
        result = backup_installation(self.spec, output, observe=self.observe)
        self.assertEqual(result["status"], "complete")
        with closing(sqlite3.connect(output / "shared.db")) as con, con:
            self.assertEqual(
                con.execute("SELECT * FROM saved_work").fetchall(), [("uncertain", "original")]
            )
        self.assertEqual(unrelated.read_bytes(), b"external original pending")
        before = (output / "receipt.json").read_bytes()
        with self.assertRaisesRegex(InstalledBackupError, "already exists.*inspect"):
            backup_installation(self.spec, output, observe=self.observe)
        self.assertEqual((output / "receipt.json").read_bytes(), before)
        self.assertTrue((output / "PENDING.json").exists())
        self.spec.unlink()
        self.assertEqual(inspect_backup(output), result)

    def test_failed_snapshot_keeps_pending_and_originals(self) -> None:
        output = self.root / "interrupted"
        with patch(
            "hermes_codex_router.installed_backup.backup_database",
            side_effect=OSError("fixture full disk"),
        ):
            with self.assertRaises(OSError):
                backup_installation(self.spec, output, observe=self.observe)
        self.assertTrue((output / "PENDING.json").exists())
        self.assertFalse((output / "receipt.json").exists())
        self.assertTrue(self.database.exists())
        with self.assertRaisesRegex(InstalledBackupError, "already exists"):
            backup_installation(self.spec, output, observe=self.observe)
        self.assertEqual(inspect_backup(output)["status"], "pending")

    def test_intent_and_parent_are_synced_before_copy_and_receipt_before_ack(self) -> None:
        from hermes_codex_router.database_backup import backup_database

        output = self.root / "durable"
        events = []
        fsync = os.fsync

        def sync(descriptor: int) -> None:
            events.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
            fsync(descriptor)

        def copy(*args, **kwargs):
            events.append("copy")
            return backup_database(*args, **kwargs)

        with (
            patch("hermes_codex_router.installed_backup.os.fsync", side_effect=sync),
            patch("hermes_codex_router.installed_backup.backup_database", side_effect=copy),
        ):
            backup_installation(self.spec, output, observe=self.observe)
        self.assertEqual(
            events,
            [output / "PENDING.json", output, self.root, "copy", output / "receipt.json", output],
        )

    def test_failed_parent_sync_retains_intent_without_copy_or_ack(self) -> None:
        output = self.root / "unsynced"
        fsync = os.fsync

        def sync(descriptor: int) -> None:
            if Path(os.readlink(f"/proc/self/fd/{descriptor}")) == self.root:
                raise OSError("fixture failed parent fsync")
            fsync(descriptor)

        with patch("hermes_codex_router.installed_backup.os.fsync", side_effect=sync):
            with self.assertRaisesRegex(OSError, "failed parent fsync"):
                backup_installation(self.spec, output, observe=self.observe)
        self.assertEqual(inspect_backup(output)["status"], "pending")
        self.assertFalse((output / "shared.db").exists())
        self.assertFalse((output / "receipt.json").exists())

    def test_config_drift_and_missing_provenance_refuse_before_writes(self) -> None:
        self.config.write_text(self.config.read_text() + "\n")
        with self.assertRaisesRegex(InstalledBackupError, "configuration.*changed"):
            backup_installation(self.spec, self.root / "bad", observe=self.observe)
        self.assertFalse((self.root / "bad").exists())

    def test_worker_cannot_claim_another_slots_identity(self) -> None:
        worker = self.document["components"][-1]
        worker["argv"][-1] = "2"
        self.write_spec()
        with self.assertRaisesRegex(InstalledBackupError, "worker command.*slot"):
            describe_installation(self.spec, observe=self.observe)

    def test_cutover_during_snapshot_retains_unfinished_operation(self) -> None:
        calls = 0

        def observe(units: tuple[str, ...]) -> dict:
            nonlocal calls
            calls += 1
            if calls > 1:
                self.units[next(iter(self.units))]["start_marker"] = "new incarnation"
            return self.units

        with self.assertRaisesRegex(InstalledBackupError, "installation changed"):
            backup_installation(self.spec, self.root / "cutover", observe=observe)
        self.assertEqual(inspect_backup(self.root / "cutover")["status"], "pending")

    def test_busy_database_refuses_without_creating_output(self) -> None:
        con = sqlite3.connect(self.database)
        try:
            con.execute("BEGIN EXCLUSIVE")
            with self.assertRaisesRegex(InstalledBackupError, "busy or unreadable"):
                backup_installation(self.spec, self.root / "busy", observe=self.observe)
            self.assertFalse((self.root / "busy").exists())
        finally:
            con.rollback()
            con.close()

    def test_unknown_database_and_incompatible_schema_refuse(self) -> None:
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute("PRAGMA user_version=57")
        with self.assertRaisesRegex(InstalledBackupError, "cannot read retained schema"):
            describe_installation(self.spec, observe=self.observe)

    def test_optional_monitor_may_be_inactive(self) -> None:
        self.units.pop("agents-projects-hub-operations-monitor.service")
        description = describe_installation(self.spec, observe=self.observe)
        monitor = next(item for item in description["components"] if item["component"] == "monitor")
        self.assertEqual(monitor["running"]["status"], "inactive")

    def test_separate_app_server_has_native_identity_without_hub_revision(self) -> None:
        unit = "agents-projects-hub-codex-app-server.service"
        executable = self.root / "native-codex"
        executable.write_bytes(b"native code fixture; never executed")
        executable.chmod(0o500)
        record = {
            "unit": unit,
            "owner": "codex/tlive",
            "required": True,
            "uses_hub_state": False,
            "argv": [str(executable), "app-server"],
            "executable": file_descriptor(executable),
        }
        self.document["auxiliary"].append(record)
        self.units[unit] = {
            "argv": record["argv"],
            "executable": str(executable),
            "start_marker": "native-1",
        }
        self.write_spec()
        result = describe_installation(self.spec, observe=self.observe)
        self.assertNotIn("release", result["auxiliary"][0])
        self.assertNotIn("argv", result["auxiliary"][0])
        self.assertEqual(result["auxiliary"][0]["owner"], "codex/tlive")

    def test_snapshot_inspection_detects_tampering(self) -> None:
        output = self.root / "tamper"
        backup_installation(self.spec, output, observe=self.observe)
        with closing(sqlite3.connect(output / "shared.db")) as con, con:
            con.execute("DELETE FROM saved_work")
        with self.assertRaisesRegex(InstalledBackupError, "snapshot database.*changed"):
            inspect_backup(output)

    def test_preactivation_manifest_is_not_installed_inventory(self) -> None:
        with self.assertRaisesRegex(InstalledBackupError, "preactivation.*not installed inventory"):
            describe_installation(self.manifest, observe=self.observe)

    def test_receipt_cannot_claim_full_recovery_or_omit_its_scope(self) -> None:
        output = self.root / "receipt-scope"
        original = backup_installation(self.spec, output, observe=self.observe)
        for change in ({"complete_recovery_set": True}, {"captured_scope": "application"}, {}):
            with self.subTest(change=change):
                receipt = {**original, **change}
                if not change:
                    receipt.pop("complete_recovery_set")
                (output / "receipt.json").write_text(json.dumps(receipt))
                with self.assertRaisesRegex(InstalledBackupError, "unsupported snapshot receipt"):
                    inspect_backup(output)

    def test_unsupported_description_reports_version(self) -> None:
        self.document["schema_version"] = 900
        self.write_spec()
        with self.assertRaisesRegex(InstalledBackupError, "unsupported.*900"):
            describe_installation(self.spec, observe=self.observe)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["release-manifest", "installed", str(self.spec)])
        self.assertEqual(code, 2)
        self.assertIn("900", json.loads(output.getvalue())["error"])

    def test_cli_inspects_original_result_without_current_installation(self) -> None:
        destination = self.root / "cli-inspection"
        backup_installation(self.spec, destination, observe=self.observe)
        self.spec.unlink()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["release-manifest", "inspect-backup", str(destination)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "complete")

    def test_installed_backup_refuses_unrelated_state(self) -> None:
        with self.assertRaisesRegex(InstalledBackupError, "state differs"):
            backup_installation(
                self.spec,
                self.root / "wrong-state",
                observe=self.observe,
                state_path=self.preupgrade,
            )
        self.assertFalse((self.root / "wrong-state").exists())

    def test_two_updates_with_schema_config_path_and_slot_changes_preserve_originals(self) -> None:
        originals = {p: p.read_bytes() for p in (self.config, self.manifest, self.preupgrade)}
        for schema, sha in ((55, "c"), (56, "d")):
            with closing(sqlite3.connect(self.database)) as con, con:
                con.execute(f"PRAGMA user_version={schema}")
                con.execute(f"CREATE TABLE schema_{schema} (value TEXT)")
            self.config = self.root / f"hub-v{schema}.json"
            self.configuration["codex_worker_count"] = schema - 53
            self.write_config()
            wheel = _wheel(
                self.root / f"next-{schema}.whl",
                version=f"0.{schema - 47}.0",
                git_sha=sha * 40,
                schema_max=56,
            )
            self.manifest = self.root / f"release-{schema}.json"
            create_deployment_manifest(
                self.manifest,
                active_artifact=wheel,
                rollback_artifact=self.current,
                configuration=self.config,
                state_backup=self.preupgrade,
            )
            runtime = self.root / "releases" / f"version-{schema}"
            site = runtime / "venv/lib/python3.11/site-packages"
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(site)
            executable = runtime / "venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture interpreter")
            (runtime / "venv/pyvenv.cfg").write_text(
                "version = 3.11.0\ninclude-system-site-packages = false\n"
            )
            for p in sorted(runtime.rglob("*"), reverse=True):
                p.chmod(0o500 if p.is_dir() or p == executable else 0o400)
            runtime.chmod(0o500)
            self.packages["active"] = site / "hermes_codex_router"
            self.document["components"] = []
            self.units = {}
            self.document["configuration"] = file_descriptor(self.config)
            for component, instance in (
                ("controller", "project-hub-controller"),
                ("monitor", "operations-monitor"),
                ("sender", "telegram-outbox-sender"),
            ):
                self.add_component(component, instance)
            for slot in range(1, schema - 52):
                self.add_component(
                    "provider_worker", "codex-worker" if slot == 1 else f"codex-worker-{slot}"
                )
            self.write_spec()
            receipt = backup_installation(
                self.spec, self.root / f"snapshot-{schema}", observe=self.observe
            )
            self.assertEqual(
                receipt["installation"]["databases"]["shared"]["schema_version"], schema
            )
            self.assertEqual(len(receipt["installation"]["components"]), schema - 50)
            with closing(sqlite3.connect(self.root / f"snapshot-{schema}/shared.db")) as con, con:
                self.assertEqual(
                    con.execute("SELECT result FROM saved_work").fetchone(), ("original",)
                )
            for path, data in originals.items():
                self.assertEqual(path.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
