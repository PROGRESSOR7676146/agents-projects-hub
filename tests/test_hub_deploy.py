"""Offline tests for the independent Hub deployment building blocks."""

import copy
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts import hub_deploy


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.old = "/opt/example hub/old"
        self.new = "/home/example/runtime/new"
        self.config = {
            "agents": [
                {"runtime": "claude", "executable": self.old + "/bin/claude"},
                {"runtime": "codex", "executable": "/usr/bin/codex"},
            ],
            "claude_file_permissions": {
                "runtime_roots": ["/usr", self.old, self.old + "/venv"],
                "python_executable": self.old + "/venv/bin/python",
                "hook_code_root": self.old + "/venv/lib",
                "user_releases": [{"root": self.old, "sha256": "a" * 64}],
            },
            "unrelated": {"value": "keep"},
        }

    def test_structural_rewrite_preserves_unrelated_values_and_input(self):
        original = copy.deepcopy(self.config)
        result = hub_deploy.rewrite_claude_config(self.config, self.old, self.new, "b" * 64)
        self.assertEqual(self.config, original)
        self.assertEqual(result["unrelated"], original["unrelated"])
        self.assertEqual(result["agents"][1], original["agents"][1])
        self.assertEqual(result["agents"][0]["executable"], self.new + "/bin/claude")
        self.assertEqual(
            result["claude_file_permissions"]["runtime_roots"],
            ["/usr", self.new, self.new + "/venv"],
        )
        self.assertEqual(
            result["claude_file_permissions"]["user_releases"],
            [{"root": self.new, "sha256": "b" * 64}],
        )

    def test_host_profile_rewrites_only_the_cli_executable(self):
        self.config["claude_file_permissions"] = {
            "profile": "host",
            "provider_home": "/home/example/claude-host-sessions",
        }
        result = hub_deploy.rewrite_claude_config(self.config, self.old, self.new, "b" * 64)
        self.assertEqual(result["agents"][0]["executable"], self.new + "/bin/claude")
        self.assertEqual(result["claude_file_permissions"], self.config["claude_file_permissions"])

    def test_root_owned_runtime_without_previous_pin(self):
        del self.config["claude_file_permissions"]["user_releases"]
        result = hub_deploy.rewrite_claude_config(self.config, self.old, self.new, "b" * 64)
        self.assertEqual(result["claude_file_permissions"]["user_releases"][0]["root"], self.new)

    def test_stray_old_root_is_refused(self):
        self.config["unrelated"]["value"] = "prefix " + self.old
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.rewrite_claude_config(self.config, self.old, self.new, "b" * 64)

    def test_unexpected_additional_pin_is_refused(self):
        self.config["claude_file_permissions"]["user_releases"].append(
            {"root": "/home/example/another", "sha256": "c" * 64}
        )
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.rewrite_claude_config(self.config, self.old, self.new, "b" * 64)

    def test_dropin_rewrites_exec_and_environment_directives(self):
        body = (
            '[Service]\nExecStart=\nExecStart="'
            + self.old
            + '/venv/bin/hub" --config "old.json"\nExecStartPost='
            + self.old
            + f'/bin/check\nExecStartPre="{self.old}/bin/pre"\n'
            + f'ExecStopPost="{self.old}/bin/post"\n'
            + f'Environment="SOURCE={self.old}/src" OTHER=unchanged\n'
        )
        result = hub_deploy.rewrite_dropin(body, self.old, self.new, "old.json", "new.json")
        self.assertIn('"' + self.new + '/venv/bin/hub"', result)
        self.assertIn('"new.json"', result)
        self.assertIn(f'Environment="SOURCE={self.new}/src" OTHER=unchanged', result)
        self.assertIn(f'ExecStartPre="{self.new}/bin/pre"', result)
        self.assertIn(f'ExecStopPost="{self.new}/bin/post"', result)

    def test_dropin_stray_reference_is_refused(self):
        body = "[Service]\nExecStart=" + self.old + "/bin/hub\nWorkingDirectory=" + self.old + "\n"
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.rewrite_dropin(body, self.old, self.new, "old.json", "new.json")

    def test_environment_requires_a_runtime_path_at_assignment_start(self):
        for value in (
            f'X="prefix{self.old}/src"',
            f'X="{self.old}-other/src"',
            f'"X=prefix SOURCE={self.old}/src"',
        ):
            with self.subTest(value=value), self.assertRaises(hub_deploy.Refused):
                hub_deploy.rewrite_dropin(
                    "Environment=" + value + "\n", self.old, self.new, "old.json", "new.json"
                )
        self.assertFalse(hub_deploy._environment_has_root(f'X="{self.old}-other/src"', self.old))

    def test_dropin_rejects_runtime_in_argument(self):
        body = f'ExecStart="{self.old}/bin/hub" --other "{self.old}/data"\n'
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.rewrite_dropin(body, self.old, self.new, "old.json", "new.json")

    def test_non_boundary_prefix_is_not_rewritten(self):
        self.config["claude_file_permissions"]["runtime_roots"].append(self.old + "-other")
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.rewrite_claude_config(self.config, self.old, self.new, "b" * 64)


class EvidenceTests(unittest.TestCase):
    def test_schema_gates_forward_and_downgrade(self):
        hub_deploy.check_schema({"schema_max": 7}, {"schema_max": 7}, 7)
        hub_deploy.check_schema({"schema_max": 7}, {"schema_max": 8}, 7, {"schema_max": 8})
        for old, new, live, rollback, message in [
            (7, 8, 7, None, "--rollback-release"),
            (7, 8, 7, {"schema_max": 9}, "rollback release schema"),
            (7, 6, 7, None, "downgrade"),
            (7, 7, 8, None, "old release schema"),
            (True, 1, 1, None, "schema evidence"),
        ]:
            with (
                self.subTest(old=old, new=new, live=live),
                self.assertRaisesRegex(hub_deploy.Refused, message),
            ):
                hub_deploy.check_schema({"schema_max": old}, {"schema_max": new}, live, rollback)

    def test_plan_digest_detects_any_change(self):
        plan = {"schema_version": 1, "files": [{"before": "a", "after": "b"}]}
        bound = hub_deploy.bind_plan(plan)
        self.assertEqual(hub_deploy.verify_plan(bound), plan)
        bound["plan"]["files"][0]["after"] = "different"
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.verify_plan(bound)

    def test_release_checksums_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {
                "requirements-release.lock": b"lock",
                "requirements-build.lock": b"build lock",
                "uv.lock": b"uv lock",
                "agents_projects_hub-1.2.3-py3-none-any.whl": b"wheel",
                "agents_projects_hub-1.2.3.tar.gz": b"archive",
            }
            info = {
                "package_version": "1.2.3",
                "git_sha": "a" * 40,
                "clean_tree": True,
                "schema_max": 7,
                "artifacts": [
                    {
                        "name": name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                    for name, content in data.items()
                ],
            }
            data["release-info.json"] = json.dumps(info).encode()
            for name, content in data.items():
                (root / name).write_bytes(content)
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                    for name, content in data.items()
                )
            )
            report, wheel = hub_deploy.verify_assets(root, "v1.2.3")
            self.assertEqual(report, info)
            self.assertEqual(wheel.name, "agents_projects_hub-1.2.3-py3-none-any.whl")
            info["artifacts"][0]["sha256"] = "0" * 64
            data["release-info.json"] = json.dumps(info).encode()
            (root / "release-info.json").write_bytes(data["release-info.json"])
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                    for name, content in data.items()
                )
            )
            with self.assertRaises(hub_deploy.Refused):
                hub_deploy.verify_assets(root, "v1.2.3")
            info["artifacts"][0]["sha256"] = hashlib.sha256(
                data[info["artifacts"][0]["name"]]
            ).hexdigest()
            data["release-info.json"] = json.dumps(info).encode()
            (root / "release-info.json").write_bytes(data["release-info.json"])
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                    for name, content in data.items()
                )
            )
            wheel.write_bytes(b"changed")
            with self.assertRaises(hub_deploy.Refused):
                hub_deploy.verify_assets(root, "v1.2.3")


class DiscoveryTests(unittest.TestCase):
    def test_discover_root_with_spaces_and_mixed_layout_refusal(self):
        first = '[Service]\nExecStart=\nExecStart="/opt/example hub/old/venv/bin/hub" run\n'
        second = '[Service]\nExecStart="/opt/example hub/old/venv/bin/hub" worker\n'
        self.assertEqual(hub_deploy.discover_dropin_root([first, second]), "/opt/example hub/old")
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.discover_dropin_root([first, second.replace("/old/", "/other/")])

    def test_discovery_refuses_ambiguous_and_missing_commands(self):
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.discover_dropin_root(["[Service]\nExecStart=/usr/bin/other\n"])
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.discover_dropin_root(
                [
                    "[Service]\nExecStart=/opt/example/old/venv/bin/hub\nExecStart=/opt/example/new/venv/bin/hub\n"
                ]
            )

    def test_pin_verification_required_for_user_owned_runtime(self):
        old = "/home/example/releases/old"
        config = {"claude_file_permissions": {"user_releases": [{"root": old, "sha256": "a" * 64}]}}
        seen = []
        hub_deploy.verify_config_pin(
            config, old, lambda root, digest: seen.append((root, digest)) or True, user_owned=True
        )
        self.assertEqual(seen, [(old, "a" * 64)])
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.verify_config_pin(config, old, lambda *_: False, user_owned=True)
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.verify_config_pin(
                {"claude_file_permissions": {}}, old, lambda *_: True, user_owned=True
            )
        hub_deploy.verify_config_pin(
            {"claude_file_permissions": {}}, "/opt/example/old", lambda *_: False, user_owned=False
        )


class StageFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old_sha, self.new_sha = "a" * 40, "b" * 40
        self.old = self.base / "opt layout" / self.old_sha
        self.old.mkdir(parents=True)
        self.units = self.base / "units"
        self.units.mkdir()
        self.configs = self.base / "configs"
        self.configs.mkdir()
        self.releases = self.base / "releases"
        self.releases.mkdir()
        self.ops = self.base / "ops"
        self.ops.mkdir(mode=0o700)
        self.native = self.base / "native"
        self.native.write_bytes(b"native")
        self.hub = self.base / "hub.json"
        self.hub.write_text("{}")
        self.manifest = self.base / "manifest.json"
        self.manifest.write_text("{}")
        self.db = self.base / "state.db"
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version=7")
        self.install = {
            "schema_version": 1,
            "units_dir": str(self.units),
            "hub_config": str(self.hub),
            "claude_config_dir": str(self.configs),
            "claude_config_pattern": "claude-*.json",
            "managed_dropin_name": "override.conf",
            "codex_manifest": str(self.manifest),
            "state_db": str(self.db),
            "releases_dir": str(self.releases),
            "ops_dir": str(self.ops),
            "native_claude": str(self.native),
            "native_sha256": hashlib.sha256(b"native").hexdigest(),
            "restart_order": ["worker.service", "controller.service"],
            "config_only_units": [],
            "controller_unit": "controller.service",
            "untouched_units": ["codex.service", "tlive.service"],
            "monitor_service": "monitor.service",
            "monitor_timer": "monitor.timer",
            "github_repo": "example/example",
        }
        self.input = self.base / "installation.json"
        self.write_input()
        self.write_layout()
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.make_assets("v1.0.0", self.old_sha)
        self.make_assets("v2.0.0", self.new_sha)
        self.calls = []

    def write_input(self):
        self.input.write_text(json.dumps(self.install))
        self.input.chmod(0o600)

    def write_layout(self, pinned=False):
        config = {
            "agents": [{"runtime": "claude", "executable": str(self.old / "bin/claude")}],
            "claude_file_permissions": {
                "python_executable": str(self.old / "venv/bin/python"),
                "hook_code_root": str(self.old / "venv/lib"),
                "runtime_roots": [str(self.old)],
            },
        }
        if pinned:
            config["claude_file_permissions"]["user_releases"] = [
                {"root": str(self.old), "sha256": "c" * 64}
            ]
        (self.configs / f"claude-{self.old_sha[:7]}.json").write_text(json.dumps(config))
        self.hub.write_text(json.dumps(config))
        for unit in self.install["restart_order"]:
            directory = self.units / (unit + ".d")
            directory.mkdir(exist_ok=True)
            (directory / "override.conf").write_text(
                f'[Service]\nExecStart=\nExecStart="{self.old}/venv/bin/hub" --config "{self.configs}/claude-{self.old_sha[:7]}.json"\n'
            )

    def make_assets(self, tag, sha, schema=7):
        directory = self.assets / tag
        directory.mkdir()
        version = tag[1:]
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for unit in hub_deploy.BASE_UNITS:
                content = f"template {unit}\n".encode()
                entry = tarfile.TarInfo(f"agents_projects_hub-{version}/systemd/{unit}")
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
                live = self.units / unit
                if not live.exists():
                    live.write_bytes(content if unit != hub_deploy.BASE_UNITS[0] else b"old unit\n")
        files = {
            "requirements-release.lock": b"lock",
            "requirements-build.lock": b"build",
            "uv.lock": b"uv",
            f"agents_projects_hub-{version}-py3-none-any.whl": b"wheel",
            f"agents_projects_hub-{version}.tar.gz": archive.getvalue(),
        }
        info = {
            "package_version": version,
            "git_sha": sha,
            "clean_tree": True,
            "schema_max": schema,
            "artifacts": [
                {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                for name, data in files.items()
            ],
        }
        files["release-info.json"] = json.dumps(info).encode()
        for name, data in files.items():
            (directory / name).write_bytes(data)
        (directory / "SHA256SUMS").write_text(
            "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items())
        )

    def runner(self, argv, **kwargs):
        argv = list(map(str, argv))
        self.calls.append(argv)
        if argv[:3] == ["gh", "release", "download"]:
            source = self.assets / argv[3]
            target = Path(argv[argv.index("--dir") + 1])
            for path in source.iterdir():
                shutil.copyfile(path, target / path.name)
            return SimpleNamespace(stdout="")
        if argv[:3] == ["gh", "release", "list"]:
            return SimpleNamespace(
                stdout=json.dumps([{"tagName": "v1.0.0", "targetCommitish": self.old_sha}])
            )
        if argv[:2] == ["gh", "api"]:
            return SimpleNamespace(stdout=json.dumps({"sha": self.old_sha}))
        if argv[:2] == ["uv", "venv"]:
            bindir = Path(argv[-1]) / "bin"
            bindir.mkdir(parents=True)
            for name in ("python", "python3", "python3.12"):
                (bindir / name).symlink_to("/usr/bin/python3.12")
        if "validate-hub" in argv:
            return SimpleNamespace(stdout=json.dumps({"ok": True}))
        if "release-info" in argv:
            sha = (
                self.old_sha
                if str(self.old) in argv[0]
                else ("d" * 40 if "d" * 40 in argv[0] else self.new_sha)
            )
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "git_sha": sha,
                        "clean_tree": True,
                        "package_version": "1.0.0"
                        if sha == self.old_sha
                        else ("3.0.0" if sha == "d" * 40 else "2.0.0"),
                    }
                )
            )
        if "hermes_codex_router.runtime_release" in argv:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "ok": True,
                        "root": argv[-1] if "--sha256" not in argv else argv[-3],
                        "sha256": "c" * 64,
                    }
                )
            )
        return SimpleNamespace(stdout="")


class StageTests(StageFixture):
    def test_input_validation(self):
        self.assertEqual(hub_deploy.load_installation(self.input)["schema_version"], 1)
        for key, value in [
            ("schema_version", True),
            ("units_dir", "relative"),
            ("restart_order", ["worker.service", "worker.service"]),
            ("config_only_units", ["worker.service"]),
            ("config_only_units", ["other.service", "other.service"]),
            ("native_sha256", "x"),
            ("controller_unit", "worker.service"),
            ("github_repo", "../bad"),
        ]:
            original = self.install[key]
            self.install[key] = value
            self.write_input()
            with self.subTest(key=key), self.assertRaises(hub_deploy.Refused):
                hub_deploy.load_installation(self.input)
            self.install[key] = original
        self.write_input()
        self.input.chmod(0o644)
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.load_installation(self.input)

    def host_config(self, extra=None):
        config = self.configs / f"claude-{self.old_sha[:7]}.json"
        data = json.loads(config.read_text())
        data["claude_file_permissions"] = {
            "profile": "host",
            "provider_home": "/home/example/claude-host-sessions",
            **(extra or {}),
        }
        config.write_text(json.dumps(data))

    def test_host_profile_with_extra_permission_keys_is_refused(self):
        self.host_config({"runtime_roots": ["/usr"]})
        install = hub_deploy.load_installation(self.input)
        with self.assertRaisesRegex(hub_deploy.Refused, "host permissions"):
            hub_deploy.discover_installation(
                install, self.runner, lambda unit, root: True, lambda root: 0
            )

    def test_discovery_full_layout_and_residency(self):
        install = hub_deploy.load_installation(self.input)
        found = hub_deploy.discover_installation(
            install, self.runner, lambda unit, root: True, lambda root: 0
        )
        self.assertEqual((found["root"], found["git_sha"]), (str(self.old), self.old_sha))
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.discover_installation(
                install, self.runner, lambda unit, root: False, lambda root: 0
            )
        self.old = self.releases / self.old_sha
        self.old.mkdir()
        self.write_layout(pinned=True)
        found = hub_deploy.discover_installation(
            install, self.runner, lambda unit, root: True, lambda root: 0
        )
        self.assertEqual(found["root"], str(self.old))
        config = self.configs / f"claude-{self.old_sha[:7]}.json"
        data = json.loads(config.read_text())
        data["agents"][0]["executable"] = "/opt/example/other/bin/claude"
        config.write_text(json.dumps(data))
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.discover_installation(
                install, self.runner, lambda unit, root: True, lambda root: 0
            )

    def test_prepare_and_schema_gate_end_to_end(self):
        install = hub_deploy.load_installation(self.input)
        found = hub_deploy.discover_installation(
            install, self.runner, lambda unit, root: True, lambda root: 0
        )
        prepared = hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner)
        self.assertEqual(prepared["git_sha"], self.new_sha)
        self.assertEqual(prepared["tree_sha256"], "c" * 64)
        self.assertEqual(Path(prepared["candidate_assets"]).stat().st_mode & 0o777, 0o700)
        uv_calls = [call for call in self.calls if call[0] == "uv"]
        self.assertEqual(len(uv_calls), 3)
        self.assertIn("--require-hashes", uv_calls[1])
        self.assertIn("--no-deps", uv_calls[2])
        self.assertEqual((self.releases / self.new_sha / "bin/claude").read_bytes(), b"native")
        self.assertEqual(
            (self.releases / self.new_sha / "venv/bin/python").readlink(), Path("python3.12")
        )
        uv_count = sum(call[0] == "uv" for call in self.calls)
        again = hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner)
        self.assertEqual(again["tree_sha256"], prepared["tree_sha256"])
        self.assertEqual(uv_count, sum(call[0] == "uv" for call in self.calls))
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version=8")
        with self.assertRaisesRegex(hub_deploy.Refused, "schema"):
            hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner)

    def test_discovery_refuses_conflicting_pin_and_config(self):
        install = hub_deploy.load_installation(self.input)
        self.old = self.releases / self.old_sha
        self.old.mkdir()
        self.write_layout(pinned=True)
        config = self.configs / f"claude-{self.old_sha[:7]}.json"
        data = json.loads(config.read_text())
        data["claude_file_permissions"]["user_releases"][0]["sha256"] = "d" * 64
        config.write_text(json.dumps(data))
        with self.assertRaisesRegex(hub_deploy.Refused, "verification"):
            hub_deploy.discover_installation(install, self.runner, lambda *_: True)
        data["claude_file_permissions"]["user_releases"][0]["sha256"] = "c" * 64
        config.write_text(json.dumps(data))
        (self.configs / "claude-other.json").write_text(json.dumps(data))
        dropin = self.units / "worker.service.d/override.conf"
        dropin.write_text(
            dropin.read_text() + f'ExecStartPost=/bin/true "{self.configs}/claude-other.json"\n'
        )
        with self.assertRaisesRegex(hub_deploy.Refused, "ambiguous"):
            hub_deploy.discover_installation(install, self.runner, lambda *_: True)

    def test_schema_old_asset_and_download_integrity_are_required(self):
        install = hub_deploy.load_installation(self.input)
        found = hub_deploy.discover_installation(install, self.runner, lambda *_: True, lambda _: 0)

        def wrong_lookup(*_):
            return "v2.0.0"

        with self.assertRaisesRegex(hub_deploy.Refused, "old release identity"):
            hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner, wrong_lookup)
        self.assertFalse((self.releases / self.new_sha).exists())

    def test_old_release_schema_mismatch_blocks_build(self):
        install = hub_deploy.load_installation(self.input)
        found = hub_deploy.discover_installation(install, self.runner, lambda *_: True, lambda _: 0)
        directory = self.assets / "v1.0.0"
        info_path = directory / "release-info.json"
        info = json.loads(info_path.read_text())
        info["schema_max"] = 6
        info_path.write_text(json.dumps(info))
        checksums = directory / "SHA256SUMS"
        lines = checksums.read_text().splitlines()
        checksums.write_text(
            "\n".join(
                f"{hashlib.sha256(info_path.read_bytes()).hexdigest()}  release-info.json"
                if line.endswith("  release-info.json")
                else line
                for line in lines
            )
            + "\n"
        )
        with self.assertRaisesRegex(hub_deploy.Refused, "schema"):
            hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner)
        self.assertFalse((self.releases / self.new_sha).exists())
        (self.assets / "v2.0.0" / "requirements-release.lock").write_text("tampered")
        with self.assertRaisesRegex(hub_deploy.Refused, "digest"):
            hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner)
        self.assertFalse((self.releases / self.new_sha).exists())

    def test_partial_build_is_never_rebuilt_and_native_is_checked(self):
        install = hub_deploy.load_installation(self.input)
        found = hub_deploy.discover_installation(install, self.runner, lambda *_: True, lambda _: 0)
        self.native.write_bytes(b"tampered")
        with self.assertRaisesRegex(hub_deploy.Refused, "native"):
            hub_deploy.prepare_stage(install, found, "v2.0.0", self.runner)
        self.native.write_bytes(b"native")
        (self.releases / self.new_sha).mkdir()

        def failed_pin(argv, **kwargs):
            if "hermes_codex_router.runtime_release" in argv:
                raise hub_deploy.Refused("unsealed")
            return self.runner(argv, **kwargs)

        with self.assertRaisesRegex(hub_deploy.Refused, "unsealed"):
            hub_deploy.prepare_stage(install, found, "v2.0.0", failed_pin)
        self.assertFalse(any(call[0] == "uv" for call in self.calls))

    def test_cli_plan_records_and_rechecks_exact_evidence(self):
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha, "other": "keep"}))
        output = io.StringIO()
        with redirect_stdout(output):
            result = hub_deploy.main(
                ["plan", "--installation", str(self.input), "--release", "v2.0.0"],
                runner=self.runner,
                inspect_process=lambda *_: True,
                runtime_owner=lambda _: 0,
                python=Path("/usr/bin/python3"),
            )
        self.assertEqual(result, 0)
        plan_dir = Path(json.loads(output.getvalue())["plan_dir"])
        self.assertEqual(plan_dir.stat().st_mode & 0o777, 0o700)
        plan = hub_deploy.verify_saved_plan(plan_dir)
        self.assertEqual(plan["old_runtime"]["git_sha"], self.old_sha)
        self.assertEqual(plan["new_runtime"]["tree_sha256"], "c" * 64)
        self.assertEqual(plan["restart_order"], self.install["restart_order"])
        self.assertEqual(plan["untouched_units"], self.install["untouched_units"])
        entries = plan["files"]
        bases = [x for x in entries if x["kind"] == "base_unit" and x["changed"]]
        self.assertEqual(len(bases), 0)
        self.assertFalse(any(x["kind"] == "base_unit" and not x["changed"] for x in entries))
        manifest = next(x for x in entries if x["kind"] == "manifest")
        candidate = json.loads((plan_dir / manifest["after"]["copy"]).read_text())
        self.assertEqual(candidate, {"hub_git_sha": self.new_sha, "other": "keep"})
        validations = [c for c in self.calls if "validate-hub" in c]
        self.assertEqual(len(validations), 2)
        self.assertTrue(
            all(c[0].startswith(str(self.releases / self.new_sha)) for c in validations)
        )
        after = plan_dir / manifest["after"]["copy"]
        saved = after.read_bytes()
        after.write_bytes(b"tampered")
        with self.assertRaisesRegex(hub_deploy.Refused, "digest"):
            hub_deploy.verify_saved_plan(plan_dir)
        after.write_bytes(saved)
        plan_path = plan_dir / "plan.json"
        saved_plan = plan_path.read_bytes()
        bound = json.loads(saved_plan)
        bound["plan"]["restart_order"].reverse()
        plan_path.write_text(json.dumps(bound))
        with self.assertRaisesRegex(hub_deploy.Refused, "plan digest"):
            hub_deploy.verify_saved_plan(plan_dir)
        plan_path.write_bytes(saved_plan)
        self.manifest.write_text("changed")
        with self.assertRaisesRegex(hub_deploy.Refused, "live file"):
            hub_deploy.verify_saved_plan(plan_dir)

    def test_candidate_validation_failure_refuses_plan(self):
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))

        def failing(argv, **kwargs):
            if "validate-hub" in argv:
                raise hub_deploy.Refused("candidate rejected")
            return self.runner(argv, **kwargs)

        with redirect_stderr(io.StringIO()) as error:
            result = hub_deploy.main(
                ["plan", "--installation", str(self.input), "--release", "v2.0.0"],
                runner=failing,
                inspect_process=lambda *_: True,
                runtime_owner=lambda _: 0,
                python=Path("/usr/bin/python3"),
            )
        self.assertEqual(result, 1)
        self.assertIn("candidate rejected", error.getvalue())

    def test_change_during_candidate_validation_refuses_plan(self):
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))

        def changing(argv, **kwargs):
            if "validate-hub" in argv:
                self.manifest.write_text("changed during planning")
            return self.runner(argv, **kwargs)

        with redirect_stderr(io.StringIO()) as error:
            result = hub_deploy.main(
                ["plan", "--installation", str(self.input), "--release", "v2.0.0"],
                runner=changing,
                inspect_process=lambda *_: True,
                runtime_owner=lambda _: 0,
                python=Path("/usr/bin/python3"),
            )
        self.assertEqual(result, 1)
        self.assertIn("live file changed", error.getvalue())

    def test_all_old_runtime_dropins_are_included(self):
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        monitor = self.units / "monitor.service.d"
        monitor.mkdir()
        (monitor / "override.conf").write_text(f"ExecStart={self.old}/venv/bin/hub monitor\n")
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        plan = hub_deploy.verify_saved_plan(
            hub_deploy.create_plan(self.install, old, new, self.runner)
        )
        self.assertIn(str(monitor / "override.conf"), [e["path"] for e in plan["files"]])
        unknown = self.units / "other.service.d"
        unknown.mkdir()
        (unknown / "override.conf").write_text(f"ExecStart={self.old}/venv/bin/hub\n")
        with self.assertRaisesRegex(hub_deploy.Refused, "consumer"):
            hub_deploy.create_plan(self.install, old, new, self.runner)
        with self.assertRaisesRegex(hub_deploy.Refused, "consumer"):
            hub_deploy.discover_installation(
                self.install, self.runner, lambda *_: True, lambda _: 0
            )

    def test_unscoped_runtime_dropin_is_refused(self):
        path = self.units / "unrelated.service.d/override.conf"
        path.parent.mkdir()
        path.write_text(f"ExecStart={self.old}/venv/bin/hub\n")
        with self.assertRaisesRegex(hub_deploy.Refused, "consumer"):
            hub_deploy.discover_installation(
                self.install, self.runner, lambda *_: True, lambda _: 0
            )

    def test_shared_runtime_dropin_refuses_active_instance_outside_restart_order(self):
        worker = self.units / "worker.service.d/override.conf"
        body = worker.read_text()
        worker.unlink()
        self.install["restart_order"] = ["worker@one.service", "controller.service"]
        template = self.units / "worker@.service.d/override.conf"
        template.parent.mkdir()
        template.write_text(body)
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)

        def active_instance(argv, **kwargs):
            if "list-units" in argv:
                return SimpleNamespace(
                    stdout="worker@other.service loaded active running fixture\n"
                )
            return self.runner(argv, **kwargs)

        with self.assertRaisesRegex(hub_deploy.Refused, "shared"):
            hub_deploy.create_plan(self.install, old, new, active_instance)

    def test_changed_protected_base_unit_is_refused(self):
        self.install["untouched_units"][0] = "agents-projects-hub-codex-app-server.service"
        (self.units / self.install["untouched_units"][0]).write_text("old protected unit\n")
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)
        with self.assertRaisesRegex(hub_deploy.Refused, "protected"):
            hub_deploy.create_plan(self.install, old, new, self.runner)

    def test_codex_priority_template_is_protected_until_staged(self):
        unit = "agents-projects-hub-codex-app-server.service"
        self.install["untouched_units"][0] = unit
        candidate = (Path(__file__).resolve().parents[1] / "systemd" / unit).read_bytes()
        before = candidate.replace(b"Nice=10\n", b"", 1)
        self.assertNotEqual(before, candidate)
        (self.units / unit).write_bytes(before)
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)
        templates = hub_deploy._templates(new)
        templates[unit] = candidate
        with patch.object(hub_deploy, "_templates", return_value=templates):
            with self.assertRaisesRegex(hub_deploy.Refused, "protected unit template changed"):
                hub_deploy.create_plan(self.install, old, new, self.runner)
            (self.units / unit).write_bytes(candidate)
            plan = hub_deploy.verify_saved_plan(
                hub_deploy.create_plan(self.install, old, new, self.runner)
            )
        entry = next(item for item in plan["files"] if item["path"] == str(self.units / unit))
        self.assertEqual(entry["kind"], "base_guard")
        self.assertFalse(entry["changed"])

    def test_template_and_instance_dropins_are_planned_and_stray_ignored(self):
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        worker = self.units / "worker.service.d/override.conf"
        body = worker.read_text()
        worker.unlink()
        self.install["restart_order"] = [
            "worker@one.service",
            "worker@two.service",
            "controller.service",
        ]
        template = self.units / "worker@.service.d/override.conf"
        instance = self.units / "worker@two.service.d/override.conf"
        stray = self.units / "unrelated.service.d/override.conf"
        for path in (template, instance, stray):
            path.parent.mkdir()
        template.write_text(body)
        instance.write_text(body)
        stray.write_text("[Service]\nEnvironment=FIXTURE=yes\n")
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)

        def running_instances(argv, **kwargs):
            if "list-units" in argv:
                return SimpleNamespace(
                    stdout="worker@one.service loaded active running fixture\nworker@two.service loaded active running fixture\n"
                )
            return self.runner(argv, **kwargs)

        plan = hub_deploy.verify_saved_plan(
            hub_deploy.create_plan(self.install, old, new, running_instances)
        )
        self.assertIn(str(template), [entry["path"] for entry in plan["files"]])
        template.unlink()
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.discover_installation(
                self.install, self.runner, lambda *_: True, lambda _: 0
            )

    def test_mixed_runtime_dropin_is_refused(self):
        path = self.units / "worker.service.d/override.conf"
        path.write_text(path.read_text() + f'ExecStartPost="{self.base}/other/venv/bin/hub"\n')
        with self.assertRaisesRegex(hub_deploy.Refused, "mixed runtime"):
            hub_deploy.discover_installation(
                self.install, self.runner, lambda *_: True, lambda _: 0
            )

    def test_managed_symlink_is_refused(self):
        path = self.units / "worker.service.d/override.conf"
        target = path.with_name("target.conf")
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(hub_deploy.Refused, "unsafe file"):
            hub_deploy.discover_installation(
                self.install, self.runner, lambda *_: True, lambda _: 0
            )

    def test_config_without_revision_in_filename_is_updated_in_place(self):
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        previous = self.configs / f"claude-{self.old_sha[:7]}.json"
        fixed = self.configs / "claude-current.json"
        previous.rename(fixed)
        for unit in self.install["restart_order"]:
            path = self.units / (unit + ".d") / "override.conf"
            path.write_text(path.read_text().replace(previous.name, fixed.name))
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        plan = hub_deploy.verify_saved_plan(
            hub_deploy.create_plan(self.install, old, new, self.runner)
        )
        config = next(e for e in plan["files"] if e["kind"] == "claude_config")
        self.assertEqual(config["path"], str(fixed))
        self.assertTrue(config["changed"])


class ServiceManagerTests(unittest.TestCase):
    def test_dependency_alias_of_untouched_unit_is_refused(self):
        class Manager:
            def property(self, unit, name):
                if name == "Names":
                    return "codex.service codex-alias.service" if unit.startswith("codex") else unit
                if name == "Id":
                    return "codex.service" if unit.startswith("codex") else unit
                return "codex-alias.service" if (unit, name) == ("worker.service", "Wants") else ""

        with self.assertRaisesRegex(hub_deploy.Refused, "dependency"):
            hub_deploy._check_dependencies(Manager(), ["worker.service"], ["codex.service"])

    def test_oneshot_config_only_helper_may_only_be_started(self):
        def manager(kind, remain, edge):
            class Manager:
                def property(self, unit, name):
                    if name in ("Id", "Names"):
                        return unit
                    if unit == "socket-dir.service" and name == "Type":
                        return kind
                    if unit == "socket-dir.service" and name == "RemainAfterExit":
                        return remain
                    return "socket-dir.service" if (unit, name) == ("worker.service", edge) else ""

            return Manager()

        helper = ("socket-dir.service",)
        hub_deploy._check_dependencies(
            manager("oneshot", "no", "Wants"), ["worker.service"], ["codex.service"], helper
        )
        for kind, remain, edge, untouched, config_only in (
            ("oneshot", "yes", "Wants", ["codex.service"], helper),
            ("simple", "no", "Wants", ["codex.service"], helper),
            ("oneshot", "no", "Wants", ["codex.service", "socket-dir.service"], ()),
            ("oneshot", "no", "PropagatesStopTo", ["codex.service"], helper),
            ("oneshot", "no", "Upholds", ["codex.service"], helper),
        ):
            with (
                self.subTest(kind=kind, remain=remain, edge=edge, config_only=config_only),
                self.assertRaisesRegex(hub_deploy.Refused, "untouched unit: socket-dir"),
            ):
                hub_deploy._check_dependencies(
                    manager(kind, remain, edge), ["worker.service"], untouched, config_only
                )

    def test_helper_sharing_an_untouched_identity_is_refused(self):
        class Manager:
            def property(self, unit, name):
                if name == "Id":
                    return "codex.service" if unit in ("codex.service", "helper.service") else unit
                if name == "Names":
                    if unit in ("codex.service", "helper.service"):
                        return "codex.service helper.service"
                    return unit
                if name == "Type":
                    return "oneshot"
                if name == "RemainAfterExit":
                    return "no"
                return "helper.service" if (unit, name) == ("worker.service", "Wants") else ""

        for untouched in (["codex.service"], ["helper.service"]):
            with (
                self.subTest(untouched=untouched),
                self.assertRaisesRegex(hub_deploy.Refused, "untouched unit"),
            ):
                hub_deploy._check_dependencies(
                    Manager(), ["worker.service"], untouched, ("helper.service",)
                )

    def test_missing_dependency_property_is_not_treated_as_empty(self):
        manager = hub_deploy.ServiceManager(lambda argv: SimpleNamespace(stdout=""))
        with self.assertRaisesRegex(hub_deploy.Refused, "dependency property"):
            manager.property("worker.service", "Upholds")

    def test_candidate_dependency_change_is_refused_before_window(self):
        before = b"[Unit]\nDescription=worker\n[Service]\nExecStart=/old/bin\n"
        after = b"[Unit]\nDescription=worker\nWants=codex.service\n[Service]\nExecStart=/new/bin\n"
        with self.assertRaisesRegex(hub_deploy.Refused, "candidate.*dependency"):
            hub_deploy._check_candidate_unit(before, after)
        hub_deploy._check_candidate_unit(before, before.replace(b"/old", b"/new"))
        hub_deploy._check_candidate_unit(
            before,
            before.replace(b"Description=worker\n", b"Description=worker\nAfter=codex.service\n"),
        )

    def test_dependency_graph_propagation_and_order_only(self):
        class Manager:
            def __init__(self, edges):
                self.edges = edges
                self.reads = set()

            def property(self, unit, name):
                self.reads.add((unit, name))
                if name in ("Id", "Names"):
                    return unit
                return self.edges.get((unit, name), "")

        for edges in (
            {("worker.service", "Wants"): "codex.service"},
            {
                ("worker.service", "Requires"): "middle.service",
                ("middle.service", "Upholds"): "codex.service",
            },
            {("worker.service", "ConsistsOf"): "codex.service"},
            {
                ("codex.service", "PartOf"): "worker.service",
                ("worker.service", "ConsistsOf"): "codex.service",
            },
        ):
            with (
                self.subTest(edges=edges),
                self.assertRaisesRegex(hub_deploy.Refused, "dependency"),
            ):
                hub_deploy._check_dependencies(
                    Manager(edges), ["worker.service"], ["codex.service"]
                )
        manager = Manager({("worker.service", "After"): "codex.service"})
        hub_deploy._check_dependencies(manager, ["worker.service"], ["codex.service"])
        for unit in ("worker.service", "codex.service"):
            for name in (
                "Wants",
                "Requires",
                "Requisite",
                "BindsTo",
                "PartOf",
                "Upholds",
                "WantedBy",
                "RequiredBy",
                "RequisiteOf",
                "BoundBy",
                "ConsistsOf",
                "UpheldBy",
                "PropagatesStopTo",
                "StopPropagatedFrom",
                "Conflicts",
                "ConflictedBy",
            ):
                self.assertIn((unit, name), manager.reads)

    def test_new_dependency_properties_use_strict_systemctl_reads(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            name = next(item for item in argv if item.startswith("--property=")).split("=", 1)[1]
            return SimpleNamespace(stdout=name + "=fixture.service\n")

        manager = hub_deploy.ServiceManager(runner)
        for name in ("PropagatesStopTo", "StopPropagatedFrom", "Conflicts", "ConflictedBy"):
            self.assertEqual(manager.property("worker.service", name), "fixture.service")
            self.assertEqual(
                calls[-1][-4:], ["--all", "--property=" + name, "--", "worker.service"]
            )
        manager.runner = lambda argv: SimpleNamespace(stdout="")
        for name in ("PropagatesStopTo", "StopPropagatedFrom", "Conflicts", "ConflictedBy"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(hub_deploy.Refused, "dependency property"),
            ):
                manager.property("worker.service", name)

    def test_environment_is_read_through_systemctl(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return SimpleNamespace(stdout='"SOURCE=/opt/example/runtime/src"\n')

        manager = hub_deploy.ServiceManager(runner)
        self.assertTrue(
            hub_deploy._environment_has_root(
                manager.property("gateway.service", "Environment"), "/opt/example/runtime"
            )
        )
        self.assertEqual(
            calls,
            [
                [
                    "systemctl",
                    "--user",
                    "show",
                    "--property=Environment",
                    "--value",
                    "--",
                    "gateway.service",
                ]
            ],
        )

    def test_root_slice_is_not_parsed_as_an_option(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return SimpleNamespace(stdout="Id=-.slice\n")

        hub_deploy.ServiceManager(runner).property("-.slice", "Id")
        self.assertEqual(calls[0][-2:], ["--", "-.slice"])

    def test_effective_settings_and_fallback_are_checked(self):
        values = {
            "ActiveState": "inactive",
            "UnitFileState": "enabled",
            "TimeoutStartUSec": "5min",
            "StartLimitIntervalUSec": "0",
        }

        def runner(argv):
            key = next(item.split("=", 1)[1] for item in argv if item.startswith("--property="))
            return SimpleNamespace(stdout=values[key])

        manager = hub_deploy.ServiceManager(runner)
        manager.preflight_extra()
        manager.verify_effective("codex.service")
        for key, invalid in (
            ("ActiveState", "failed"),
            ("TimeoutStartUSec", "90s"),
            ("StartLimitIntervalUSec", "10s"),
        ):
            previous = values[key]
            values[key] = invalid
            with self.subTest(key=key), self.assertRaises(hub_deploy.Refused):
                if key == "ActiveState":
                    manager.preflight_extra()
                else:
                    manager.verify_effective("codex.service")
            values[key] = previous


class FakeManager:
    def __init__(self, install):
        self.install = install
        self.states = {
            u: "active"
            for u in install["restart_order"]
            + install["untouched_units"]
            + install["config_only_units"]
        }
        self.states[install["monitor_timer"]] = "active"
        self.states[install["monitor_service"]] = "inactive"
        self.calls = []
        self.fail_start = False
        self.monitor_failures = 0
        self.environment_override: str | None = None
        self.resident_calls = []
        self.effective_reads = []

    def runner(self, argv):
        return SimpleNamespace(stdout="")

    def identity(self, unit):
        return {"ActiveState": self.states[unit], "MainPID": "1", "InvocationID": "fixed"}

    def enabled(self, unit):
        return True

    def executable(self, unit):
        return "/fixture/codex"

    def preflight_extra(self):
        pass

    def verify_effective(self, app):
        pass

    def stop(self, unit):
        self.calls.append(("stop", unit))
        self.states[unit] = "inactive"

    def start(self, unit):
        self.calls.append(("start", unit))
        if unit == self.install["monitor_service"] and self.monitor_failures:
            self.monitor_failures -= 1
            raise hub_deploy.Refused("monitor failure")
        if unit == self.install["controller_unit"] and self.fail_start:
            self.fail_start = False
            raise hub_deploy.Refused("start failure")
        self.states[unit] = "active"

    def reload(self):
        self.calls.append(("reload",))

    def resident(self, unit, root):
        self.resident_calls.append((unit, root))
        return self.states[unit] == "active"

    def property(self, unit, name):
        if name in ("ExecStart", "ExecStartPost", "Environment"):
            self.effective_reads.append((unit, name))
        if name in ("Id", "Names"):
            return unit
        if name in ("ExecStart", "ExecStartPost"):
            path = Path(self.install["units_dir"]) / (unit + ".d") / "override.conf"
            return (
                " ".join(
                    line.partition("=")[2]
                    for line in path.read_text().splitlines()
                    if line.startswith(name + "=")
                )
                if path.exists()
                else ""
            )
        if name != "Environment":
            return ""
        self.calls.append(("environment", unit))
        if self.environment_override is not None:
            return self.environment_override
        path = Path(self.install["units_dir"]) / (unit + ".d") / "override.conf"
        return (
            next(
                (
                    line.partition("=")[2]
                    for line in path.read_text().splitlines()
                    if line.startswith("Environment=")
                ),
                "",
            )
            if path.exists()
            else ""
        )


class WindowTests(StageFixture):
    def setUp(self):
        super().setUp()
        self.manifest.write_text(json.dumps({"hub_git_sha": self.old_sha}))
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE provider_jobs (agent_id TEXT, status TEXT)")
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        self.manager = FakeManager(self.install)
        self.manager.states["codex.service"] = "failed"
        self.sleeps = []
        self.fail_verify_old = False

    def test_claude_settings_drift_between_configs_refuses_the_plan(self):
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        hub = json.loads(self.hub.read_text())
        drifted = copy.deepcopy(hub)
        drifted["claude_file_permissions"]["profile"] = "host"
        for changed in (drifted, {**hub, "agents": [{"runtime": "claude", "auth_route": "x"}]}):
            with self.subTest(changed=changed):
                self.hub.write_text(json.dumps(changed))
                with self.assertRaisesRegex(hub_deploy.Refused, "Claude settings differ"):
                    hub_deploy.create_plan(self.install, old, new, self.runner)
        # Agent order is not drift; a duplicate Claude identity is refused.
        first = {"runtime": "claude", "agent_id": "first", "executable": "/x"}
        second = {"runtime": "claude", "agent_id": "second", "executable": "/y"}
        self.assertEqual(
            hub_deploy.claude_policy({"agents": [first, second]}),
            hub_deploy.claude_policy({"agents": [second, first]}),
        )
        with self.assertRaisesRegex(hub_deploy.Refused, "duplicate Claude agent"):
            hub_deploy.claude_policy({"agents": [first, first]})
        # The release overlay alone (executable, runtime paths) is not drift.
        overlay = copy.deepcopy(hub)
        overlay["agents"][0]["executable"] = "/home/example/other/bin/claude"
        overlay["claude_file_permissions"]["runtime_roots"] = ["/home/example/other"]
        self.hub.write_text(json.dumps(overlay))
        hub_deploy.create_plan(self.install, old, new, self.runner)

    def test_host_profile_deploys_and_keeps_its_permissions(self):
        config = self.configs / f"claude-{self.old_sha[:7]}.json"
        data = json.loads(config.read_text())
        host = {"profile": "host", "provider_home": "/home/example/claude-host-sessions"}
        data["claude_file_permissions"] = host
        config.write_text(json.dumps(data))
        self.hub.write_text(json.dumps(data))
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        self.assertIsNone(old["tree_sha256"])
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        shutil.rmtree(self.plan)
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        self.assertEqual(self.execute()["outcome"], "ACCEPTED")
        candidate = json.loads(
            Path(
                hub_deploy.verify_saved_plan(self.plan, check_live=False)["candidate_config"]
            ).read_text()
        )
        self.assertEqual(candidate["claude_file_permissions"], host)
        self.assertTrue(candidate["agents"][0]["executable"].startswith(new["root"] + "/"))
        window = self.require_window()
        result = hub_deploy.rollback_window(
            self.install, window["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK")
        restored = json.loads(config.read_text())
        self.assertEqual(restored["claude_file_permissions"], host)
        self.assertEqual(restored["agents"][0]["executable"], data["agents"][0]["executable"])

    def require_window(self) -> dict[str, Any]:
        window = hub_deploy.last_window(self.install)
        if window is None:
            self.fail("expected a window")
        return window

    def require_status(self) -> dict[str, Any]:
        status = hub_deploy.window_status(self.install)
        if status is None:
            self.fail("expected a window status")
        return status

    def window_runner(self, argv, **kwargs):
        argv = list(map(str, argv))
        self.calls.append(argv)
        if (
            "release-info" in argv
            or "hermes_codex_router.runtime_release" in argv
            or "validate-hub" in argv
        ):
            return self.runner(argv, **kwargs)
        if "backup" in argv:
            shutil.copyfile(self.db, argv[-1])
        if "create" in argv and "release-manifest" in argv:
            Path(argv[argv.index("create") + 1]).write_text("manifest")
        if "status" in argv:
            sha = (
                self.old_sha
                if str(self.old) in argv[0]
                else ("d" * 40 if "d" * 40 in argv[0] else self.new_sha)
            )
            if sha == self.old_sha and self.fail_verify_old:
                raise hub_deploy.Refused("old convergence failed")
            component = {
                "status": "healthy",
                "release_git_sha": sha,
                "release_clean": True,
                "instance_id": "fixture",
            }
            health: dict[str, Any] = {
                k: component for k in ("controller", "monitor", "sender", "project_provisioner")
            }
            health["provider_workers"] = []
            health["deployment_revision"] = {
                "status": "converged",
                "git_sha": sha,
                "required_components": 4,
            }
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "schema_version": hub_deploy._live_schema(self.install),
                        "runtime_health": health,
                    }
                )
            )
        if "doctor" in argv:
            return SimpleNamespace(
                stdout=json.dumps({"checks": [{"name": "codex_socket", "detail": "listening"}]})
            )
        return SimpleNamespace(stdout="{}")

    def execute(self, fault=None):
        return hub_deploy.apply_window(
            self.install,
            self.plan,
            self.manager,
            self.window_runner,
            self.sleeps.append,
            load=lambda: 0,
            fault=fault,
        )

    def prepare_environment_only_worker(self):
        path = self.units / "worker.service.d/override.conf"
        original = f'[Service]\nEnvironment="SOURCE={self.old}/src"\n'
        path.write_text(original)
        self.manager = FakeManager(self.install)
        self.manager.states["codex.service"] = "failed"
        old = hub_deploy.discover_installation(
            self.install,
            self.runner,
            lambda unit, root: unit != "worker.service",
            lambda _: 0,
            manager=self.manager,
        )
        self.assertEqual(old["unit_runtime_modes"]["worker.service"], "environment")
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        return path, original, new

    def prepare_forward(self, rollback_schema=8):
        shutil.rmtree(self.assets / "v2.0.0")
        self.make_assets("v2.0.0", self.new_sha, schema=8)
        self.make_assets("v3.0.0", "d" * 40, schema=rollback_schema)
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        return old

    def test_forward_requires_verified_matching_rollback_release(self):
        old = self.prepare_forward(rollback_schema=9)
        with self.assertRaisesRegex(hub_deploy.Refused, "--rollback-release"):
            hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)
        with self.assertRaisesRegex(hub_deploy.Refused, "rollback release schema"):
            hub_deploy.prepare_stage(
                self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
            )

    def test_forward_plan_binds_schemas_and_target(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        path = hub_deploy.create_plan(self.install, old, new, self.runner)
        bound = json.loads((path / "plan.json").read_text())
        self.assertEqual(
            (bound["plan"]["state_schema_before"], bound["plan"]["state_schema"]), (7, 8)
        )
        self.assertEqual(bound["plan"]["rollback_runtime"]["tag"], "v3.0.0")
        for key in (
            "state_schema_before",
            "state_schema",
            "rollback_runtime",
            "candidate_config",
            "rollback_config",
        ):
            altered = copy.deepcopy(bound)
            altered["plan"][key] = None
            with self.subTest(key=key), self.assertRaisesRegex(hub_deploy.Refused, "digest"):
                hub_deploy.verify_plan(altered)
        self.assertTrue((self.releases / ("d" * 40) / "bin/claude").exists())

    def test_forward_backup_and_schema_selected_recovery(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        original = self.manager.start

        def migrate_then_fail(unit):
            if unit == "worker.service":
                window = self.require_window()
                rows = hub_deploy._journal_rows(self.ops / window["id"] / "journal.jsonl")
                self.assertTrue(
                    any(r.get("phase") == "after" and r.get("effect") == "backup" for r in rows)
                )
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("PRAGMA user_version=8")
                self.manager.start = original
                raise hub_deploy.Refused("failure after migration")
            original(unit)

        self.manager.start = migrate_then_fail
        result = self.execute()
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        rows = hub_deploy._journal_rows(self.ops / result["id"] / "journal.jsonl")
        self.assertEqual(
            next(r for r in rows if r["phase"] == "rollback_target")["target"], "rollback_runtime"
        )
        self.assertEqual(self.require_status()["rollback_target"], "rollback_release")
        with closing(sqlite3.connect(self.ops / result["id"] / "state.db")) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertEqual(hub_deploy._live_schema(self.install), 8)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(hub_deploy.main(["status", "--installation", str(self.input)]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(
            (report["rollback_target"], report["rollback_git_sha"]), ("rollback_release", "d" * 40)
        )
        self.assertIn("d" * 40, (self.units / "worker.service.d/override.conf").read_text())

    def test_forward_waits_for_migration_commit_after_start(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)

        def commit_on_wait(seconds):
            self.sleeps.append(seconds)
            with closing(sqlite3.connect(self.db)) as db:
                db.execute("PRAGMA user_version=8")

        result = hub_deploy.apply_window(
            self.install,
            self.plan,
            self.manager,
            self.window_runner,
            commit_on_wait,
            load=lambda: 0,
        )
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        self.assertEqual(self.sleeps, [5])

    def test_forward_config_snapshots_are_selected_by_role(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        plan = hub_deploy.verify_saved_plan(self.plan)
        entries = {e["path"]: e for e in plan["files"]}
        old_path = old["claude_config"]
        candidate, rollback = plan["candidate_config"], plan["rollback_config"]
        self.assertEqual(len({old_path, candidate, rollback}), 3)
        self.assertEqual(entries[old_path]["kind"], "source_config")
        self.assertIsNone(entries[rollback]["after"])
        self.assertIsNone(entries[candidate]["rollback"])
        validated = []

        def runner(argv, **kwargs):
            if "validate-hub" in argv:
                validated.append((argv[0], Path(argv[-1]).read_bytes()))
            return self.window_runner(argv, **kwargs)

        result = hub_deploy.apply_window(
            self.install, self.plan, self.manager, runner, self.sleeps.append, load=lambda: 0
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertEqual(len(validated), 4)
        for role, path, snapshot in (
            ("new_runtime", candidate, "after"),
            ("rollback_runtime", rollback, "rollback"),
        ):
            content = (self.plan / entries[path][snapshot]["copy"]).read_bytes()
            self.assertIn((hub_deploy._runtime_cli(plan[role]), content), validated)
        for role, snapshot, key in (
            ("new_runtime", "after", "candidate_config"),
            ("rollback_runtime", "rollback", "rollback_config"),
        ):
            altered = copy.deepcopy(plan)
            altered["candidate_config" if role == "rollback_runtime" else "rollback_config"] = (
                old_path
            )
            altered[key] = rollback if role == "new_runtime" else candidate
            with self.assertRaisesRegex(hub_deploy.Refused, "missing active config snapshot"):
                hub_deploy._validate_plan_configs(self.plan, altered, role, snapshot, key, runner)

    def test_forward_unexpected_schema_does_not_wait(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        original = self.manager.start

        def start(unit):
            original(unit)
            if unit == "controller.service":
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("PRAGMA user_version=99")

        self.manager.start = start
        self.assertEqual(self.execute()["outcome"], "RECOVERY_REQUIRED")
        self.assertEqual(self.sleeps, [])

    def test_forward_before_migration_and_unknown_schema_recovery(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        self.manager.fail_start = True
        result = self.execute()
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertEqual(self.require_status()["rollback_target"], "previous")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version=99")
        self.manager.calls.clear()
        self.assertEqual(self.require_status()["rollback_target"], "owner_required")
        result = hub_deploy.rollback_window(
            self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED")
        self.assertIn("owner decision required", result["error"])
        self.assertFalse(any(call[0] == "start" for call in self.manager.calls))

    def test_forward_start_without_migration_rolls_back_to_previous(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        result = self.execute()
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        rows = hub_deploy._journal_rows(self.ops / result["id"] / "journal.jsonl")
        self.assertEqual(
            next(r for r in rows if r["phase"] == "rollback_target")["target"], "old_runtime"
        )
        self.assertEqual(hub_deploy._live_schema(self.install), 7)

    def test_explicit_forward_rollback_selects_target_after_migration(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        original = self.manager.start

        def migrate(unit):
            if unit == "worker.service":
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("PRAGMA user_version=8")
            original(unit)

        self.manager.start = migrate
        result = self.execute()
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        self.assertEqual(self.require_status()["rollback_target"], "rollback_release")
        self.manager.start = original
        result = hub_deploy.rollback_window(
            self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertIn("d" * 40, (self.units / "worker.service.d/override.conf").read_text())

    def test_environment_only_worker_before_and_after(self):
        path, _, new = self.prepare_environment_only_worker()
        self.manager.environment_override = "SOURCE=/unrelated"
        with self.assertRaisesRegex(hub_deploy.Refused, "environment does not reference"):
            hub_deploy.discover_installation(
                self.install, self.runner, lambda *_: True, lambda _: 0, manager=self.manager
            )
        with self.assertRaisesRegex(hub_deploy.Refused, "environment does not reference"):
            self.execute()
        self.assertFalse(any(call[0] == "stop" for call in self.manager.calls))
        self.manager.environment_override = None
        self.assertEqual(self.execute()["outcome"], "ACCEPTED")
        self.assertIn(f"SOURCE={new['root']}/src", path.read_text())
        self.assertGreaterEqual(self.manager.calls.count(("environment", "worker.service")), 3)
        self.assertFalse(any(unit == "worker.service" for unit, _ in self.manager.resident_calls))

    def test_environment_only_worker_rollback_checks_old_environment(self):
        path, original, _ = self.prepare_environment_only_worker()
        self.manager.fail_start = True
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")
        self.assertEqual(path.read_text(), original)
        self.assertGreaterEqual(self.manager.calls.count(("environment", "worker.service")), 3)
        self.assertFalse(any(unit == "worker.service" for unit, _ in self.manager.resident_calls))

    def test_environment_only_worker_wrong_after_causes_rollback(self):
        path, original, _ = self.prepare_environment_only_worker()
        start = self.manager.start

        def stale_environment(unit):
            start(unit)
            if unit == "controller.service":
                self.manager.environment_override = f'SOURCE="{self.old}/src"'

        self.manager.start = stale_environment
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")
        self.assertEqual(path.read_text(), original)

    def test_environment_only_worker_wrong_after_rollback_requires_recovery(self):
        self.prepare_environment_only_worker()
        self.manager.fail_start = True
        start = self.manager.start

        def wrong_rollback_environment(unit):
            start(unit)
            if unit == "controller.service":
                self.manager.environment_override = "SOURCE=/unrelated"

        self.manager.start = wrong_rollback_environment
        self.assertEqual(self.execute()["outcome"], "RECOVERY_REQUIRED")

    def test_success_and_untouched_failed_server(self):
        result = self.execute()
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(self.manager.states["codex.service"], "failed")
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        self.assertFalse(
            any(len(c) > 1 and c[1] in self.install["untouched_units"] for c in self.manager.calls)
        )
        self.assertTrue(any("backup" in c and c[0].startswith(str(self.old)) for c in self.calls))
        self.assertTrue(
            any(
                "status" in c and c[0].startswith(str(self.releases / self.new_sha))
                for c in self.calls
            )
        )
        self.assertEqual(
            self.manager.calls[:3],
            [
                ("stop", "monitor.timer"),
                ("stop", "monitor.service"),
                ("stop", "controller.service"),
            ],
        )

    def prepare_config_only(self):
        self.install["config_only_units"] = ["codex.service", "socket-dir.service"]
        for unit in self.install["config_only_units"]:
            path = self.units / (unit + ".d") / "override.conf"
            path.parent.mkdir(exist_ok=True)
            path.write_text(
                f'[Service]\nExecStart="{self.old}/venv/bin/hub"\nExecStartPost="{self.old}/venv/bin/check"\nEnvironment="SOURCE={self.old}/src"\n'
            )
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        self.manager = FakeManager(self.install)
        self.manager.states["codex.service"] = "failed"

    def rerun_helper_outcome(self, result, active=lambda reads: "inactive"):
        """Drive the socket-dir oneshot; ``result`` sees whether the candidate is installed."""
        self.prepare_config_only()
        original_identity = self.manager.identity
        original_property = self.manager.property
        dropin = self.units / "socket-dir.service.d" / "override.conf"
        reads = []

        def identity(unit):
            value = original_identity(unit)
            if unit == "socket-dir.service":
                reads.append(unit)
                value = {**value, "InvocationID": f"run-{len(reads)}"}
            return value

        def property_value(unit, name):
            if unit == "socket-dir.service":
                values = {
                    "Type": "oneshot",
                    "RemainAfterExit": "no",
                    "SubState": "dead",
                    "Result": result(str(self.releases / self.new_sha) in dropin.read_text()),
                }
                if name == "ActiveState":
                    reads.append(name)
                    return active(len(reads))
                if name in values:
                    return values[name]
            return original_property(unit, name)

        self.manager.identity = identity
        self.manager.property = property_value
        return self.execute()["outcome"]

    def test_rerun_oneshot_helper_identity_change_is_accepted(self):
        self.assertEqual(self.rerun_helper_outcome(lambda candidate: "success"), "ACCEPTED")

    def test_running_helper_is_awaited_before_acceptance(self):
        outcome = self.rerun_helper_outcome(
            lambda candidate: "success",
            active=lambda reads: "activating" if reads < 4 else "inactive",
        )
        self.assertEqual(outcome, "ACCEPTED")
        self.assertIn(5, self.sleeps)

    def test_candidate_helper_failure_rolls_back_to_a_working_helper(self):
        outcome = self.rerun_helper_outcome(
            lambda candidate: "exit-code" if candidate else "success"
        )
        self.assertEqual(outcome, "ROLLED_BACK")

    def test_failing_rerun_oneshot_helper_is_never_accepted(self):
        # It fails for the rollback runtime too, so recovery cannot be verified.
        self.assertEqual(
            self.rerun_helper_outcome(lambda candidate: "exit-code"), "RECOVERY_REQUIRED"
        )

    def test_config_only_rewritten_verified_and_rolled_back_without_lifecycle(self):
        self.prepare_config_only()
        paths = [
            self.units / (u + ".d") / "override.conf" for u in self.install["config_only_units"]
        ]
        original = [p.read_bytes() for p in paths]
        result = self.execute()
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertTrue(all(str(self.releases / self.new_sha) in p.read_text() for p in paths))
        result = hub_deploy.rollback_window(
            self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK")
        self.assertEqual([p.read_bytes() for p in paths], original)
        for unit in self.install["config_only_units"]:
            for name in ("ExecStart", "ExecStartPost", "Environment"):
                self.assertGreaterEqual(self.manager.effective_reads.count((unit, name)), 2)
        self.assertFalse(
            any(
                c[0] in ("start", "stop", "restart", "reset")
                and c[1] in self.install["config_only_units"]
                for c in self.manager.calls
            )
        )

    def test_config_only_effective_mismatch_causes_rollback(self):
        self.prepare_config_only()
        original = self.manager.property

        def stale(unit, name):
            if unit == "socket-dir.service" and name == "ExecStartPost":
                return str(self.old) + "/venv/bin/check"
            return original(unit, name)

        self.manager.property = stale
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")

    def test_config_only_identity_change_requires_recovery(self):
        self.prepare_config_only()
        reload = self.manager.reload

        def changed():
            reload()
            self.manager.states["socket-dir.service"] = "inactive"

        self.manager.reload = changed
        self.assertEqual(self.execute()["outcome"], "RECOVERY_REQUIRED")

    def test_config_only_recovery_requires_effective_old_runtime(self):
        self.prepare_config_only()
        result = self.execute()
        original = self.manager.property

        def stale(unit, name):
            if unit == "socket-dir.service" and name == "Environment":
                return "SOURCE=/unrelated"
            return original(unit, name)

        self.manager.property = stale
        result = hub_deploy.rollback_window(
            self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED")

    def test_config_only_propagation_refused_before_effects(self):
        self.prepare_config_only()
        original = self.manager.property

        def dependency(unit, name):
            if unit == "worker.service" and name == "Wants":
                return "socket-dir.service"
            return original(unit, name)

        self.manager.property = dependency
        with self.assertRaisesRegex(hub_deploy.Refused, "dependency"):
            self.execute()
        self.assertFalse(any(c[0] in ("start", "stop", "reload") for c in self.manager.calls))

    def test_dependency_on_untouched_unit_refused_before_effects(self):
        original = self.manager.property

        def property_value(unit, name):
            if unit == "worker.service" and name == "Wants":
                return "codex.service"
            if name in ("Id", "Names"):
                return unit
            if name != "Environment":
                return ""
            return original(unit, name)

        self.manager.property = property_value
        with self.assertRaisesRegex(hub_deploy.Refused, "dependency"):
            self.execute()
        self.assertFalse(any(call[0] in ("start", "stop", "reload") for call in self.manager.calls))

    def test_quoted_escaped_dependency_names_are_traversed(self):
        # systemctl prints escaped names quoted with doubled backslashes.
        original = self.manager.property
        quoted = '"app-example\\\\x2dhub.slice"'
        unit_name = "app-example\\x2dhub.slice"
        seen = []

        def property_value(unit, name):
            seen.append(unit)
            if unit == "worker.service" and name == "Requires":
                return quoted
            if unit == unit_name and name == "Names":
                return quoted
            return original(unit, name)

        self.manager.property = property_value
        self.execute()
        self.assertIn(unit_name, seen)

    def test_malformed_quoted_dependency_property_is_refused(self):
        original = self.manager.property
        for broken in ("Requires", "Names"):

            def property_value(unit, name, broken=broken):
                if unit == "worker.service" and name == broken:
                    return '"worker.service'
                return original(unit, name)

            self.manager.property = property_value
            with (
                self.subTest(property=broken),
                self.assertRaisesRegex(hub_deploy.Refused, "malformed " + broken),
            ):
                self.execute()
            self.assertFalse(
                any(call[0] in ("start", "stop", "reload") for call in self.manager.calls)
            )

    def test_stop_propagation_and_conflicts_refused_before_effects(self):
        self.prepare_config_only()
        original = self.manager.property
        scenarios = (
            {
                ("worker.service", "PropagatesStopTo"): "middle.service",
                ("middle.service", "StopPropagatedFrom"): "worker.service",
                ("middle.service", "PropagatesStopTo"): "codex-alias.service",
                ("codex-alias.service", "StopPropagatedFrom"): "middle.service",
            },
            {("worker.service", "Conflicts"): "codex.service"},
            *(
                {
                    ("worker.service", relation): "middle.service",
                    ("middle.service", "Conflicts"): "codex.service",
                }
                for relation in ("Wants", "Requires", "BindsTo", "Upholds")
            ),
            {
                ("worker.service", "Wants"): "middle.service",
                ("middle.service", "Conflicts"): "stopper.service",
                ("stopper.service", "PropagatesStopTo"): "socket-dir.service",
            },
            {
                ("worker.service", "Requires"): "middle.service",
                ("middle.service", "ConflictedBy"): "codex-alias.service",
            },
            {
                ("worker.service", "PropagatesStopTo"): "middle.service",
                ("middle.service", "UpheldBy"): "keeper.service",
                ("middle.service", "Conflicts"): "socket-dir.service",
            },
        )
        for edges in scenarios:

            def property_value(unit, name):
                if unit in ("codex.service", "codex-alias.service"):
                    if name == "Id":
                        return "codex.service"
                    if name == "Names":
                        return "codex.service codex-alias.service"
                return edges.get((unit, name), original(unit, name))

            self.manager.property = property_value
            with (
                self.subTest(edges=edges),
                self.assertRaisesRegex(hub_deploy.Refused, "dependency"),
            ):
                self.execute()
            self.assertFalse(
                any(call[0] in ("start", "stop", "reload") for call in self.manager.calls)
            )

    def test_rollback_schema_drift_never_starts_old_runtime(self):
        result = self.execute()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version = 99")
        self.manager.calls.clear()
        result = hub_deploy.rollback_window(
            self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED")
        self.assertFalse(any(call[0] == "start" for call in self.manager.calls))

    def test_superseded_window_refused_before_service_effects(self):
        result = self.execute()
        window = Path(self.install["ops_dir"]) / result["id"]
        later = window.with_name("window-99999999999999999999-later")
        shutil.copytree(window, later)
        self.manager.calls.clear()
        with self.assertRaisesRegex(hub_deploy.Refused, "superseded"):
            hub_deploy.rollback_window(
                self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
            )
        self.assertFalse(any(call[0] in ("start", "stop", "reload") for call in self.manager.calls))

    def test_window_lineage_survives_clock_moving_backwards(self):
        first = self.execute()
        self.assertEqual(
            hub_deploy.rollback_window(
                self.install, first["id"], self.manager, self.window_runner, self.sleeps.append
            )["outcome"],
            "ROLLED_BACK",
        )
        with patch.object(hub_deploy.time, "time_ns", return_value=1):
            second = self.execute()
        self.assertEqual(self.require_window()["id"], second["id"])
        with self.assertRaisesRegex(hub_deploy.Refused, "superseded"):
            hub_deploy.rollback_window(
                self.install, first["id"], self.manager, self.window_runner, self.sleeps.append
            )

    def test_recovery_inventory_checked_before_any_mutation(self):
        result = self.execute()
        plan = hub_deploy.verify_saved_plan(self.plan, check_live=False)
        created = next(e for e in plan["files"] if e["kind"] == "claude_config")
        path = Path(created["path"])
        before = path.read_bytes()
        self.manifest.write_text("unrelated edit")
        self.manager.calls.clear()
        with self.assertRaisesRegex(hub_deploy.Refused, "unrelated file edit"):
            hub_deploy.rollback_window(
                self.install, result["id"], self.manager, self.window_runner, self.sleeps.append
            )
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(call[0] in ("start", "stop", "reload") for call in self.manager.calls))

    def test_failure_outcomes(self):
        self.manager.fail_start = True
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")
        hub_deploy.verify_saved_plan(self.plan)

    def test_rollback_restores_template_and_instance_dropins(self):
        worker = self.units / "worker.service.d/override.conf"
        original = worker.read_bytes()
        worker.unlink()
        self.install["restart_order"] = [
            "worker@one.service",
            "worker@two.service",
            "controller.service",
        ]
        template = self.units / "worker@.service.d/override.conf"
        instance = self.units / "worker@two.service.d/override.conf"
        for path in (template, instance):
            path.parent.mkdir()
            path.write_bytes(original)
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(self.install, old, "v2.0.0", self.runner)
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        self.manager = FakeManager(self.install)
        self.manager.states["codex.service"] = "failed"
        self.manager.fail_start = True
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")
        self.assertEqual(template.read_bytes(), original)
        self.assertEqual(instance.read_bytes(), original)

    def test_rollback_never_restores_database_backup(self):
        original = self.manager.start

        def start(unit):
            if unit == "controller.service":
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS after_backup (value TEXT)")
                    db.execute("INSERT INTO after_backup VALUES ('retained')")
                    db.commit()
                self.manager.start = original
                raise hub_deploy.Refused("forced start failure")
            original(unit)

        self.manager.start = start
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT value FROM after_backup").fetchone()[0], "retained")
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o644)

    def test_monitor_failure_is_incomplete_without_rollback(self):
        self.manager.monitor_failures = 2
        self.assertEqual(self.execute()["outcome"], "CORE_HEALTHY_MONITOR_INCOMPLETE")
        self.assertEqual(self.sleeps, [60])

    def test_unverified_rollback_requires_recovery(self):
        self.manager.fail_start = True
        self.fail_verify_old = True
        self.assertEqual(self.execute()["outcome"], "RECOVERY_REQUIRED")

    def test_interruption_at_every_effect_boundary_and_refusal(self):
        class Interrupted(BaseException):
            pass

        boundaries = []

        def interrupt_first(boundary):
            boundaries.append(boundary)

        result = self.execute(fault=interrupt_first)
        self.assertEqual(result["outcome"], "ACCEPTED")
        # Every boundary is exercised in a fresh, isolated installation.
        for target in range(len(boundaries)):
            with self.subTest(boundary=target):
                fixture = WindowTests(methodName="test_success_and_untouched_failed_server")
                fixture.setUp()
                try:
                    counter = [0]

                    def interrupt(_):
                        counter[0] += 1
                        if counter[0] == target + 1:
                            raise Interrupted()

                    with self.assertRaises(Interrupted):
                        fixture.execute(fault=interrupt)
                    window = fixture.require_window()
                    with self.assertRaisesRegex(hub_deploy.Refused, "rollback --window"):
                        fixture.execute()
                    result = hub_deploy.rollback_window(
                        fixture.install,
                        window["id"],
                        fixture.manager,
                        fixture.window_runner,
                        fixture.sleeps.append,
                    )
                    self.assertEqual(result["outcome"], "ROLLED_BACK")
                    hub_deploy.verify_saved_plan(fixture.plan)
                finally:
                    fixture.doCleanups()

    def test_cli_deploy_prepares_plans_and_applies(self):
        def runner(argv, **kwargs):
            if argv[0] in ("gh", "uv"):
                return self.runner(argv, **kwargs)
            return self.window_runner(argv, **kwargs)

        output = io.StringIO()
        with redirect_stdout(output):
            result = hub_deploy.main(
                ["deploy", "--installation", str(self.input), "--release", "v2.0.0"],
                runner=runner,
                inspect_process=lambda *_: True,
                runtime_owner=lambda _: 0,
                python=Path("/usr/bin/python3"),
                manager=self.manager,
                sleep=self.sleeps.append,
                load=lambda: 0,
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "ACCEPTED")

    def test_cli_apply_status_and_distinct_exit_codes(self):
        self.manager.fail_start = True
        output = io.StringIO()
        with redirect_stdout(output):
            result = hub_deploy.main(
                ["apply", "--installation", str(self.input), "--plan", str(self.plan)],
                runner=self.window_runner,
                manager=self.manager,
                sleep=self.sleeps.append,
                load=lambda: 0,
            )
        self.assertEqual(result, 2)
        outcome = json.loads(output.getvalue())
        self.assertEqual(outcome["outcome"], "ROLLED_BACK")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(hub_deploy.main(["status", "--installation", str(self.input)]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "ROLLED_BACK")

    def test_cli_refusal_prints_complete_recovery_command(self):
        class Interrupted(BaseException):
            pass

        with self.assertRaises(Interrupted):
            self.execute(fault=lambda _: (_ for _ in ()).throw(Interrupted()))
        error = io.StringIO()
        with redirect_stderr(error):
            code = hub_deploy.main(
                ["apply", "--installation", str(self.input), "--plan", str(self.plan)],
                runner=self.window_runner,
                manager=self.manager,
                sleep=self.sleeps.append,
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "rollback --window " + self.require_window()["id"] + " --installation ",
            error.getvalue(),
        )

    def test_failed_doctor_can_still_report_the_socket(self):
        def runner(argv, **kwargs):
            if "doctor" in argv:
                raise subprocess.CalledProcessError(
                    1,
                    argv,
                    output=json.dumps(
                        {
                            "ok": False,
                            "checks": [{"name": "codex_socket", "detail": "missing: fixture"}],
                        }
                    ),
                )
            return self.window_runner(argv, **kwargs)

        result = hub_deploy.apply_window(
            self.install, self.plan, self.manager, runner, self.sleeps.append, load=lambda: 0
        )
        self.assertEqual(result["outcome"], "ACCEPTED")

    def test_failed_server_does_not_require_socket_stability(self):
        calls = [0]

        def runner(argv, **kwargs):
            if "doctor" in argv:
                calls[0] += 1
                state = "listening" if calls[0] == 1 else "missing"
                return SimpleNamespace(
                    stdout=json.dumps({"checks": [{"name": "codex_socket", "detail": state}]})
                )
            return self.window_runner(argv, **kwargs)

        result = hub_deploy.apply_window(
            self.install, self.plan, self.manager, runner, self.sleeps.append, load=lambda: 0
        )
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(calls[0], 1)

    def test_torn_journal_tail_remains_recoverable(self):
        class Interrupted(BaseException):
            pass

        with self.assertRaises(Interrupted):
            self.execute(fault=lambda _: (_ for _ in ()).throw(Interrupted()))
        window = self.require_window()
        journal = self.ops / window["id"] / "journal.jsonl"
        with journal.open("ab") as stream:
            stream.write(b'{"phase":')
        self.assertEqual(self.require_window()["id"], window["id"])
        result = hub_deploy.rollback_window(
            self.install, window["id"], self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK")
        self.assertEqual(self.require_window()["state"], "ROLLED_BACK")

    def test_monitor_retry_once(self):
        self.manager.monitor_failures = 1
        self.assertEqual(self.execute()["outcome"], "ACCEPTED")
        self.assertEqual(self.sleeps, [60])

    def test_busy_recheck_after_stopping_triggers_rollback(self):
        original = self.manager.stop

        def stop(unit):
            original(unit)
            if unit == "worker.service":
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("INSERT INTO provider_jobs VALUES ('fixture', 'queued')")
                    db.commit()

        self.manager.stop = stop
        self.assertEqual(self.execute()["outcome"], "ROLLED_BACK")
        self.assertFalse(any("backup" in call for call in self.calls))

    def test_candidate_validation_mutation_is_refused_before_window(self):
        changed = [False]

        def mutate(argv, **kwargs):
            result = self.window_runner(argv, **kwargs)
            if "validate-hub" in argv and not changed[0]:
                self.manifest.write_text("unrelated edit")
                changed[0] = True
            return result

        with self.assertRaisesRegex(hub_deploy.Refused, "live file changed"):
            hub_deploy.apply_window(
                self.install, self.plan, self.manager, mutate, self.sleeps.append, load=lambda: 0
            )
        self.assertFalse(self.manager.calls)

    def test_active_server_socket_mismatch_triggers_recovery_required(self):
        self.manager.states["codex.service"] = "active"
        self.manifest.write_text(
            json.dumps({"hub_git_sha": self.old_sha, "codex_binary": {"path": "/fixture/codex"}})
        )
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        calls = [0]

        def runner(argv, **kwargs):
            if "doctor" in argv:
                calls[0] += 1
                state = "listening" if calls[0] == 1 else "missing"
                return SimpleNamespace(
                    stdout=json.dumps({"checks": [{"name": "codex_socket", "detail": state}]})
                )
            return self.window_runner(argv, **kwargs)

        result = hub_deploy.apply_window(
            self.install, self.plan, self.manager, runner, self.sleeps.append, load=lambda: 0
        )
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED")
        self.assertFalse(
            any(len(c) > 1 and c[1] in self.install["untouched_units"] for c in self.manager.calls)
        )

    def test_exclusive_lock_refuses_a_second_window(self):
        with hub_deploy._lock(self.install):
            with self.assertRaises(BlockingIOError):
                self.execute()
        self.assertFalse(self.manager.calls)

    def test_tampering_refused_before_effects(self):
        path = self.plan / "plan.json"
        data = json.loads(path.read_text())
        data["plan"]["restart_order"].reverse()
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(hub_deploy.Refused, "digest"):
            self.execute()
        self.assertFalse(self.manager.calls)


if __name__ == "__main__":
    unittest.main()
