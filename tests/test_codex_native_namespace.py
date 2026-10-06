from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.codex_native_namespace import native_namespace_argv, native_namespace_environment


class NativeNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="example-native-outer-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "example-project"
        self.project.mkdir()
        (self.project / "visible").write_text("example visible")
        self.binary = self.base / "example-binary"
        self.binary.write_text("example inert binary")
        self.actor = self.base / "example-actor.py"
        self.actor.write_text("print('example')")
        self.requirements = self.base / "example-requirements.toml"
        self.requirements.write_text("example = true")
        self.authority = self.base / "example-authority.key"
        self.authority.write_text("fictional positive control")
        self.mcp_server = self.base / "example-mcp-server.py"
        self.mcp_server.write_text("# fictional inert server")

    def argv(self, *, mcp: bool = False):
        return native_namespace_argv(
            binary=self.binary,
            actor=self.actor,
            requirements=self.requirements,
            project=self.project,
            authority=self.authority,
            mcp_server=self.mcp_server if mcp else None,
        )

    def test_outer_mounts_exclude_host_root_home_and_writable_control_aliases(self) -> None:
        argv = self.argv()
        writable = [argv[i + 1 : i + 3] for i, token in enumerate(argv) if token == "--bind"]
        self.assertEqual(
            writable,
            [[str(self.project), str(self.project)], [str(self.authority), str(self.authority)]],
        )
        sources = [argv[i + 1] for i, token in enumerate(argv) if token in ("--bind", "--ro-bind")]
        self.assertNotIn("/", sources)
        self.assertNotIn(str(self.base), sources)
        self.assertNotIn(str(Path.home()), sources)
        self.assertNotIn("/etc", sources)
        self.assertNotIn("/usr/local", sources)
        for flag in (
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--new-session",
            "--die-with-parent",
        ):
            self.assertIn(flag, argv)
        mcp_argv = self.argv(mcp=True)
        read_only = [
            mcp_argv[i + 1 : i + 3] for i, token in enumerate(mcp_argv) if token == "--ro-bind"
        ]
        self.assertIn([str(self.mcp_server), "/opt/example-native/mcp_server.py"], read_only)

    def test_environment_is_synthetic_and_contains_no_inherited_provider_credentials(self) -> None:
        env = native_namespace_environment()
        self.assertEqual(env["HOME"], "/home/example")
        self.assertEqual(env["CODEX_HOME"], "/home/example/.codex")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(
            set(env),
            {
                "HOME",
                "CODEX_HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_DATA_HOME",
                "XDG_STATE_HOME",
                "PATH",
                "LANG",
                "LC_ALL",
                "RUST_LOG",
            },
        )

    def test_real_namespace_hides_host_files_and_control_sources_but_keeps_positive_controls(
        self,
    ) -> None:
        bwrap = Path("/usr/bin/bwrap")
        if not bwrap.is_file():
            if os.environ.get("HUB_REQUIRE_NAMESPACE_TESTS") == "1":
                self.fail("required namespace runtime is unavailable")
            self.skipTest("namespace runtime is unavailable")
        outside = self.base / "example-outside-service"
        outside.mkdir()
        (outside / "authority.key").write_text("fictional outside sentinel")
        paths = {
            "project": str(self.project),
            "authority": str(self.authority),
            "outside": str(outside / "authority.key"),
            "binary_source": str(self.binary),
            "requirements_source": str(self.requirements),
            "actor_source": str(self.actor),
            "mcp_source": str(self.mcp_server),
        }
        self.actor.write_text(
            "import json, pathlib, os\n"
            + "paths = "
            + repr(paths)
            + "\n"
            + "result = {}\n"
            + "for name in ('outside', 'binary_source', 'requirements_source', 'actor_source', 'mcp_source'):\n"
            + "    result[name] = pathlib.Path(paths[name]).exists()\n"
            + "for name, path in [('project_read', pathlib.Path(paths['project'])/'visible'), ('authority_read', pathlib.Path(paths['authority']))]:\n"
            + "    try: path.read_bytes(); result[name] = True\n"
            + "    except OSError: result[name] = False\n"
            + "for name, path in [('project_write', pathlib.Path(paths['project'])/'write'), ('authority_write', pathlib.Path(paths['authority'])), ('binary_write', pathlib.Path('/usr/local/bin/example-codex')), ('requirements_write', pathlib.Path('/etc/codex/requirements.toml')), ('actor_write', pathlib.Path('/opt/example-native/actor.py')), ('mcp_write', pathlib.Path('/opt/example-native/mcp_server.py'))]:\n"
            + "    try: path.write_text('example mutation'); result[name] = True\n"
            + "    except OSError: result[name] = False\n"
            + "result['home'] = os.environ['HOME']\n"
            + "result['cwd'] = str(pathlib.Path.cwd())\n"
            + "print(json.dumps(result))\n"
        )
        process = subprocess.run(
            self.argv(mcp=True),
            env=native_namespace_environment(),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if process.returncode and any(
            reason in process.stderr
            for reason in (
                "Operation not permitted",
                "Permission denied",
                "No permissions to create",
            )
        ):
            if os.environ.get("HUB_REQUIRE_NAMESPACE_TESTS") == "1":
                self.fail("required namespace operation was denied")
            self.skipTest("namespace operation is unavailable")
        self.assertEqual(process.returncode, 0, "fictional namespace actor failed")
        result = json.loads(process.stdout)
        self.assertEqual(
            result,
            {
                "outside": False,
                "binary_source": False,
                "requirements_source": False,
                "actor_source": False,
                "mcp_source": False,
                "project_read": True,
                "project_write": True,
                "authority_read": True,
                "authority_write": True,
                "binary_write": False,
                "requirements_write": False,
                "actor_write": False,
                "mcp_write": False,
                "home": "/home/example",
                "cwd": str(self.project),
            },
        )


if __name__ == "__main__":
    unittest.main()
