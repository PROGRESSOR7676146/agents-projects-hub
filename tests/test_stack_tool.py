from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stack.py"


class StackToolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hub-stack-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        spec = importlib.util.spec_from_file_location("stack_tool", SCRIPT)
        assert spec and spec.loader
        self.tool: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.log = self.root / "actions.jsonl"
        self.failure_file = self.root / "fail"
        self.database = self.root / "retained-state"
        self.database.write_text("accepted work\n")
        guard = self.root / "guard.py"
        guard.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            f"log = Path({str(self.log)!r})\n"
            "with log.open('a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
            f"fail = Path({str(self.failure_file)!r})\n"
            "sys.exit(int(fail.exists() and fail.read_text() == ' '.join(sys.argv[1:])))\n"
        )
        components = []
        for name in ("proxy", "client"):
            directory = self.root / name
            releases = directory / "releases"
            for version in ("old", "new"):
                release = releases / version
                release.mkdir(parents=True)
                (release / "version").write_text(version)
                (release / "version").chmod(0o444)
                release.chmod(0o555)
            (directory / "current").symlink_to(releases / "old", target_is_directory=True)
            components.append(
                {
                    "id": name,
                    "releases": str(releases),
                    "link": str(directory / "current"),
                    "target": "new",
                    "group": "core",
                }
            )
        command = [sys.executable, str(guard)]
        self.document = {
            "schema_version": 1,
            "state_dir": str(self.root / "state"),
            "components": components,
            "groups": [
                {
                    "id": "core",
                    "units": ["example-proxy.service", "example-client.service"],
                    "preflight": [command + ["idle"]],
                    "check": [command + ["compatible", "{proxy}", "{client}"]],
                    "health": [command + ["healthy", "{proxy}", "{client}"]],
                }
            ],
        }
        self.manifest = self.root / "manifest.json"
        self.save_manifest()
        self.systemctl: list[list[str]] = []

    def edit(self, path: Path, text: str) -> None:
        path.chmod(0o644)
        path.write_text(text)
        path.chmod(0o444)

    def save_manifest(self) -> None:
        self.manifest.write_text(json.dumps(self.document))
        self.manifest.chmod(0o600)

    def invoke(self, *args: str) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        real_run = subprocess.run

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            if argv[0] == "systemctl":
                self.systemctl.append(argv[1:])
                return subprocess.CompletedProcess(argv, 0)
            return real_run(argv, **kwargs)

        with patch.object(self.tool.subprocess, "run", side_effect=run), redirect_stdout(output):
            code = self.tool.main([str(self.manifest), *args])
        return code, json.loads(output.getvalue())

    def plan(self) -> str:
        code, report = self.invoke("plan")
        self.assertEqual(code, 0, report)
        return str(report["digest"])

    def targets(self) -> list[str]:
        return [Path(item["link"]).resolve().name for item in self.document["components"]]

    def actions(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_plan_is_read_only_and_binds_artifacts_and_current_links(self) -> None:
        first = self.plan()
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(self.systemctl, [])
        self.edit(self.root / "client/releases/new/version", "changed")
        self.assertNotEqual(self.plan(), first)

    def test_group_switch_and_rollback_preserve_retained_work(self) -> None:
        digest = self.plan()
        code, report = self.invoke("apply", "--confirm", digest)
        self.assertEqual(code, 0, report)
        self.assertEqual(self.targets(), ["new", "new"])
        self.assertEqual(
            self.systemctl,
            [
                ["--user", "stop", "example-client.service", "example-proxy.service"],
                ["--user", "start", "example-proxy.service", "example-client.service"],
            ],
        )
        self.database.write_text("accepted work\nnew accepted work\n")
        code, report = self.invoke("rollback", "--confirm", digest)
        self.assertEqual(code, 0, report)
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertIn("new accepted work", self.database.read_text())
        previous = list(self.systemctl)
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 0)
        self.assertEqual(self.systemctl, previous)
        self.assertNotEqual(self.plan(), digest)

    def test_staging_gate_failure_does_not_stop_services_or_flip_links(self) -> None:
        self.failure_file.write_text(
            "compatible "
            + " ".join(str(self.root / name / "releases/new") for name in ("proxy", "client"))
        )
        code, _ = self.invoke("apply", "--confirm", self.plan())
        self.assertNotEqual(code, 0)
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertEqual(self.systemctl, [])

    def test_health_failure_restores_whole_group_and_checks_old_health(self) -> None:
        self.failure_file.write_text(
            "healthy "
            + " ".join(str(self.root / name / "releases/new") for name in ("proxy", "client"))
        )
        code, report = self.invoke("apply", "--confirm", self.plan())
        self.assertEqual(code, 1, report)
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertEqual(
            self.actions()[-1][1:],
            [str(self.root / name / "releases/old") for name in ("proxy", "client")],
        )

    def test_later_group_failure_restores_groups_in_reverse_order(self) -> None:
        first = self.document["groups"][0]
        first["units"] = ["example-proxy.service"]
        second = json.loads(json.dumps(first))
        second["id"] = "client"
        second["units"] = ["example-client.service"]
        second["health"][0][2] = "healthy-client"
        self.document["groups"].append(second)
        self.document["components"][1]["group"] = "client"
        self.save_manifest()
        self.failure_file.write_text(
            "healthy-client "
            + " ".join(str(self.root / name / "releases/new") for name in ("proxy", "client"))
        )
        code, report = self.invoke("apply", "--confirm", self.plan())
        self.assertEqual(code, 1, report)
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertEqual(
            self.systemctl,
            [
                ["--user", action, unit]
                for unit in (
                    "example-proxy.service",
                    "example-client.service",
                    "example-client.service",
                    "example-proxy.service",
                )
                for action in ("stop", "start")
            ],
        )

    def test_stale_confirmation_refuses_before_any_command(self) -> None:
        digest = self.plan()
        self.edit(self.root / "proxy/releases/new/version", "different")
        code, _ = self.invoke("apply", "--confirm", digest)
        self.assertEqual(code, 2)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.systemctl, [])

    def test_crash_after_first_flip_can_only_rollback_and_retains_record(self) -> None:
        digest = self.plan()
        flip = self.tool.flip_link
        count = 0

        def interrupted(link: Path, target: Path) -> None:
            nonlocal count
            flip(link, target)
            count += 1
            if count == 1:
                raise SystemExit("fictional process loss")

        with patch.object(self.tool, "flip_link", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.invoke("apply", "--confirm", digest)
        self.assertEqual(self.targets(), ["new", "old"])
        self.assertEqual(self.invoke("apply", "--confirm", self.plan())[0], 2)
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 0)
        self.assertEqual(self.targets(), ["old", "old"])

    def test_rollback_refuses_external_link_drift_before_stopping_services(self) -> None:
        digest = self.plan()
        self.assertEqual(self.invoke("apply", "--confirm", digest)[0], 0)
        link = self.root / "client/current"
        link.unlink()
        link.symlink_to(self.root)
        before = list(self.systemctl)
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 2)
        self.assertEqual(self.systemctl, before)

    def test_rollback_failure_is_resumable_and_blocks_new_switches(self) -> None:
        digest = self.plan()
        self.assertEqual(self.invoke("apply", "--confirm", digest)[0], 0)
        self.failure_file.write_text(
            "healthy "
            + " ".join(str(self.root / name / "releases/old") for name in ("proxy", "client"))
        )
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 1)
        self.assertEqual(self.invoke("apply", "--confirm", self.plan())[0], 2)
        self.failure_file.unlink()
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 0)

    def test_shared_service_requires_one_group_and_private_manifest(self) -> None:
        other = dict(self.document["groups"][0], id="other")
        self.document["groups"].append(other)
        self.save_manifest()
        self.assertEqual(self.invoke("plan")[0], 2)
        self.document["groups"].pop()
        self.save_manifest()
        self.manifest.chmod(0o644)
        self.assertEqual(self.invoke("plan")[0], 2)

    def test_manifest_and_plan_have_no_implicit_live_actions(self) -> None:
        self.document["components"][0]["id"] = "hermes"
        self.save_manifest()
        self.assertEqual(self.invoke("plan")[0], 2)

    def test_writable_release_and_corrupt_record_are_refused(self) -> None:
        artifact = self.root / "proxy/releases/new/version"
        artifact.chmod(0o644)
        self.assertEqual(self.invoke("plan")[0], 2)
        artifact.chmod(0o444)
        digest = self.plan()
        self.assertEqual(self.invoke("apply", "--confirm", digest)[0], 0)
        path = self.root / "state/switch.json"
        record = json.loads(path.read_text())
        record["plan"]["entries"][0]["before_digest"] = "0" * 64
        path.write_text(json.dumps(record))
        before = list(self.systemctl)
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 2)
        self.assertEqual(self.systemctl, before)

    def test_concurrent_operation_is_refused_and_record_stays_intact(self) -> None:
        digest = self.plan()
        with self.tool.locked(self.document):
            code, _ = self.invoke("apply", "--confirm", digest)
        self.assertEqual(code, 2)
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertEqual(self.systemctl, [])

    def test_rollback_can_restore_when_candidate_directory_is_missing(self) -> None:
        digest = self.plan()
        self.assertEqual(self.invoke("apply", "--confirm", digest)[0], 0)
        for name in ("proxy", "client"):
            directory = self.root / name / "releases/new"
            directory.chmod(0o755)
            (directory / "version").unlink()
            directory.rmdir()
        self.assertEqual(self.invoke("rollback", "--confirm", digest)[0], 0)
        self.assertEqual(self.targets(), ["old", "old"])

    def test_manifest_change_and_unknown_record_refuse_service_actions(self) -> None:
        digest = self.plan()
        self.document["groups"][0]["units"].append("example-other.service")
        self.save_manifest()
        self.assertEqual(self.invoke("apply", "--confirm", digest)[0], 2)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.systemctl, [])
        with self.tool.locked(self.document):
            record = self.root / "state/switch.json"
            record.write_text('{"status":"unknown"}')
            record.chmod(0o600)
        self.assertEqual(self.invoke("plan")[0], 2)

    def test_native_hard_exit_after_flip_is_recovered_by_separate_cli(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        manager = binary / "systemctl"
        manager.write_text("#!/usr/bin/python3\n")
        manager.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
        }
        digest = self.plan()
        runner = (
            "import importlib.util, os\n"
            f"s = importlib.util.spec_from_file_location('stack_tool', {str(SCRIPT)!r})\n"
            "v = importlib.util.module_from_spec(s); s.loader.exec_module(v)\n"
            "original = v.flip_link\n"
            "def flip(link, target):\n"
            "    original(link, target)\n"
            "    os._exit(93)\n"
            "v.flip_link = flip\n"
            f"v.main({[str(self.manifest), 'apply', '--confirm', digest]!r})\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", runner],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 93, completed.stderr)
        self.assertEqual(self.targets(), ["new", "old"])
        record = json.loads((self.root / "state/switch.json").read_text())
        self.assertEqual(record["status"], "applying")
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.manifest), "rollback", "--confirm", digest],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self.targets(), ["old", "old"])

    def test_stop_failure_restores_before_targets_and_starts_old_units(self) -> None:
        real = self.tool.run_command
        failed = False

        def run(argv: list[str]) -> None:
            nonlocal failed
            if argv[:3] == ["systemctl", "--user", "stop"] and not failed:
                failed = True
                raise RuntimeError("fictional partial stop")
            real(argv)

        with patch.object(self.tool, "run_command", side_effect=run):
            code, report = self.invoke("apply", "--confirm", self.plan())
        self.assertEqual(code, 1, report)
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertIn(
            ["--user", "start", "example-proxy.service", "example-client.service"], self.systemctl
        )

    def test_native_cli_uses_owned_fake_manager_for_apply_and_rollback(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        manager = binary / "systemctl"
        manager.write_text(
            "#!/usr/bin/python3\nimport json, sys\nfrom pathlib import Path\n"
            f"with Path({str(self.root / 'manager-actions')!r}).open('a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        )
        manager.chmod(0o755)
        environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")}
        digest = self.plan()
        for action in ("apply", "rollback"):
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(self.manifest), action, "--confirm", digest],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self.targets(), ["old", "old"])
        self.assertEqual(len((self.root / "manager-actions").read_text().splitlines()), 4)

    def test_launch_resolves_version_once_before_later_import(self) -> None:
        for version in ("old", "new"):
            release = self.root / "proxy/releases" / version
            release.chmod(0o755)
            (release / "run.py").write_text(
                "from pathlib import Path\nimport time\n"
                f"ready = Path({str(self.root / 'ready')!r})\n"
                "ready.touch()\n"
                f"go = Path({str(self.root / 'go')!r})\n"
                "while not go.exists(): time.sleep(0.01)\n"
                "print((Path(__file__).parent / 'version').read_text())\n"
            )
            (release / "python").symlink_to(Path(sys.executable).resolve())
            (release / "run.py").chmod(0o444)
            release.chmod(0o555)
        process = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                str(self.manifest),
                "launch",
                "proxy",
                "python",
                "{dir}/run.py",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            import time

            deadline = time.monotonic() + 10
            while not (self.root / "ready").exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail_test_output(process)
                time.sleep(0.01)
            self.assertTrue((self.root / "ready").exists())
            link = self.root / "proxy/current"
            link.unlink()
            link.symlink_to(self.root / "proxy/releases/new")
            (self.root / "go").touch()
            output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(output.strip(), "old")
        finally:
            (self.root / "go").touch()
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def fail_test_output(self, process: subprocess.Popen[str]) -> None:
        output, error = process.communicate(timeout=5)
        self.fail(f"fixture failed: {output} {error}")
