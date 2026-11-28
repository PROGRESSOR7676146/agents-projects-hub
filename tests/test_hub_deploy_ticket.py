import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_hub_deploy import StageFixture

from scripts import hub_deploy as deploy
from scripts import hub_deploy_ticket as ticket


class TicketTests(StageFixture):
    def setUp(self):
        super().setUp()
        self.write_layout(pinned=True)
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        self.old_state = deploy.discover_installation(self.install, self.runner, lambda *_: True)
        new = deploy.prepare_stage(self.install, self.old_state, "v2.0.0", self.runner)
        self.plan_dir = deploy.create_plan(self.install, self.old_state, new, self.runner)
        self.plan_digest = json.loads((self.plan_dir / "plan.json").read_bytes())["sha256"]
        self.manifest_dir = self.ops / "window-example"
        self.manifest_dir.mkdir()
        self.release_manifest = self.manifest_dir / deploy.MANIFEST
        self.snapshot = self.manifest_dir / "state.sqlite3"
        self.snapshot.write_bytes(b"snapshot")
        self.rollback = self.base / "older.whl"
        self.rollback.write_bytes(b"older")
        self.seen = []

    def read(self):
        return ticket.project(self.install, self.plan_dir, self.plan_digest, self.read_command)

    def read_command(self, argv):
        self.seen.append(argv)
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout="no\n")
        if argv[0] == "busctl":
            if "call" in argv:
                path = "/org/freedesktop/systemd1/unit/" + argv[-1].replace(".", "_")
                return SimpleNamespace(stdout=json.dumps({"type": "o", "data": [path]}))
            worker = argv[-3].endswith("worker_service")
            path = self.old_state["claude_config"] if worker else self.install["hub_config"]
            cli = str(self.old / "venv/bin/agents-projects-hub")
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "type": "a(sasasttttuii)",
                        "data": [[cli, [cli, "worker" if worker else "controller", path], []]],
                    }
                )
            )
        if argv[5:7] == ["release-manifest", "backup-inputs"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "ok": True,
                        "registry": {"path": str(self.base / "projects.json"), "sha256": "a" * 64},
                        "databases": {"shared": {"path": str(self.db), "schema_version": 7}},
                    }
                )
            )
        if argv[5:7] == ["release-manifest", "verify"]:
            return SimpleNamespace(stdout=self.release_manifest.read_text())
        return self.runner(argv)

    def accepted_manifest(self):
        plan = deploy.verify_saved_plan(self.plan_dir)
        old = Path(plan["old_wheel"])
        data = {
            "ok": True,
            "configuration": ticket.file_ref(self.hub),
            "active_artifact": {"git_sha": self.old_sha, **ticket.file_ref(old)},
            "rollback_artifact": {"git_sha": "d" * 40, **ticket.file_ref(self.rollback)},
            "state_backup": {**ticket.file_ref(self.snapshot), "schema_version": 7},
        }
        self.write_private(self.release_manifest, deploy.canonical_document(data))
        self.write_private(self.manifest_dir / "journal.jsonl", b'{"phase":"ACCEPTED"}\n')
        self.write_private(
            self.manifest_dir / "metadata.json",
            deploy.canonical_document(
                {
                    "plan_dir": str(self.plan_dir),
                    "plan_sha256": self.plan_digest,
                }
            ),
        )

    @staticmethod
    def write_private(path, data):
        path.write_bytes(data)
        path.chmod(0o600)

    def test_projection_binds_existing_backup_separately_from_next_rollback(self):
        self.accepted_manifest()
        bound = self.read()
        result = bound["ticket"]
        self.assertEqual(bound, self.read())
        self.assertEqual(result["plan_sha256"], self.plan_digest)
        self.assertEqual(
            result["before"]["installation_manifest"], ticket.file_ref(self.release_manifest)
        )
        self.assertEqual(result["before"]["rollback_artifact"]["git_sha"], "d" * 40)
        self.assertEqual(result["rollback"]["git_sha"], self.old_sha)
        self.assertEqual(result["target"]["git_sha"], self.new_sha)
        self.assertEqual(result["before"]["databases"], result["after"]["databases"])
        self.assertNotEqual(result["before"]["consumers"], result["after"]["consumers"])
        self.assertNotIn("manifest", result["after"])
        self.assertNotIn("snapshot", result["after"])

    def test_no_accepted_baseline_refuses(self):
        self.snapshot.unlink()
        self.manifest_dir.rmdir()
        with self.assertRaisesRegex(deploy.Refused, "accepted"):
            self.read()

    def test_effective_service_drift_changes_ticket(self):
        self.accepted_manifest()
        original = self.read()
        read_command = self.read_command

        def changed(argv):
            result = read_command(argv)
            if argv[-1] == "ExecStartEx":
                response = json.loads(result.stdout)
                response["data"][0][1].append("--changed")
                return SimpleNamespace(stdout=json.dumps(response))
            return result

        updated = ticket.project(self.install, self.plan_dir, self.plan_digest, changed)
        self.assertNotEqual(original["sha256"], updated["sha256"])
        self.assertNotEqual(original["ticket"]["before_sha256"], updated["ticket"]["before_sha256"])

    def test_wrong_plan_and_live_config_drift_refuse(self):
        self.accepted_manifest()
        with self.assertRaisesRegex(deploy.Refused, "plan digest"):
            ticket.project(self.install, self.plan_dir, "0" * 64, self.read_command)
        self.hub.write_text("{}")
        with self.assertRaisesRegex(deploy.Refused, "live file"):
            self.read()

    def test_plan_rejects_source_asset_drift_even_with_changed_catalog(self):
        self.accepted_manifest()
        plan = deploy.verify_saved_plan(self.plan_dir)
        directory = Path(plan["new_wheel"]).parent
        artifact = directory / "requirements-release.lock"
        artifact.write_text("changed")
        expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
        checksums = directory / "SHA256SUMS"
        checksums.write_text(
            "\n".join(
                f"{expected}  {artifact.name}" if line.endswith(artifact.name) else line
                for line in checksums.read_text().splitlines()
            )
        )
        with self.assertRaisesRegex(deploy.Refused, "asset catalog"):
            self.read()


class BackupReceiptTests(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run_id = "20261128T021505Z-12345678"
        self.run = self.root / self.run_id
        self.run.mkdir(mode=0o700)
        (self.run / "bundle").mkdir(mode=0o700)
        self.baseline = "a" * 64
        self.success = self.run / "SUCCESS.json"
        self.manifest = self.run / "bundle" / "manifest.json"

    def publish(self, baseline=None, covered=True):
        coverage = {"satisfies_update_artifact_precondition": covered}
        manifest = {
            "format": 1,
            "hub_baseline_sha256": self.baseline if baseline is None else baseline,
            "attachment_coverage": coverage,
        }
        TicketTests.write_private(self.manifest, deploy.canonical_document(manifest))
        data = {
            "run_id": self.run_id,
            "hub_snapshot": "c" * 64,
            "home_snapshot": "d" * 64,
            "manifest_sha256": ticket.file_ref(self.manifest)["sha256"],
            "attachment_coverage": coverage,
            "completed_at": 1_700_000_000,
        }
        TicketTests.write_private(self.success, deploy.canonical_document(data))

    def test_exact_success_binds_file_bytes(self):
        self.publish()
        verified = ticket.verify_backup(self.root, self.run_id, self.baseline)
        self.assertEqual(verified, ticket.file_ref(self.success)["sha256"])
        self.publish(baseline="0" * 64)
        with self.assertRaisesRegex(deploy.Refused, "archived baseline"):
            ticket.verify_backup(self.root, self.run_id, self.baseline)

    def test_legacy_receipt_and_misbound_run_refuse(self):
        self.publish()
        data = json.loads(self.success.read_bytes())
        manifest = {"format": 1, "attachment_coverage": data["attachment_coverage"]}
        TicketTests.write_private(self.manifest, deploy.canonical_document(manifest))
        data["manifest_sha256"] = ticket.file_ref(self.manifest)["sha256"]
        TicketTests.write_private(self.success, deploy.canonical_document(data))
        with self.assertRaisesRegex(deploy.Refused, "archived baseline"):
            ticket.verify_backup(self.root, self.run_id, self.baseline)
        data["run_id"] = "20261128T021505Z-87654321"
        TicketTests.write_private(self.success, deploy.canonical_document(data))
        with self.assertRaisesRegex(deploy.Refused, "receipt differs"):
            ticket.verify_backup(self.root, self.run_id, self.baseline)

    def test_passive_or_degraded_or_tampered_receipt_refuses(self):
        self.publish(covered=False)
        with self.assertRaisesRegex(deploy.Refused, "coverage"):
            ticket.verify_backup(self.root, self.run_id, self.baseline)
        self.publish()
        self.manifest.write_text("{}")
        with self.assertRaisesRegex(deploy.Refused, "archive manifest"):
            ticket.verify_backup(self.root, self.run_id, self.baseline)
        self.success.rename(self.run / "BACKUP.json")
        with self.assertRaisesRegex(deploy.Refused, "success receipt"):
            ticket.verify_backup(self.root, self.run_id, self.baseline)
