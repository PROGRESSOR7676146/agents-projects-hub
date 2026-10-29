"""Synthetic namespace paths must not retarget readonly runtime or hooks."""

from __future__ import annotations

import dataclasses
import errno
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.process_namespace as namespace
from tests.fd_fixture import assert_descriptor_cleanup
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_mapped_host_root,
    require_namespace_runtime,
)


def setUpModule() -> None:
    require_mapped_host_root()


class NamespaceTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-namespace-topology-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.home = self.base / "session"
        self.private = self.base / "authority"
        for path in (self.project, self.home, self.private):
            path.mkdir(mode=0o700)
        (self.project / ".git").mkdir()
        self.runtime = namespace.NamespaceRuntime(
            bwrap_executable=Path(shutil.which("bwrap") or "/usr/bin/true"),
            executable=Path("/usr/bin/true"),
            runtime_roots=(Path("/usr/bin/true"),),
        )
        self.config = namespace.ProcessNamespaceConfig(
            runtime=self.runtime,
            project_root=self.project,
            session_home=self.home,
            private_paths=(self.private,),
        )

    def test_root_is_readonly_after_all_mount_setup(self) -> None:
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            self.config.wrap(["/usr/bin/true"], {}, self.project) as launch,
        ):
            cutoff = launch.argv.index("--chdir")
            self.assertEqual(launch.argv[cutoff - 2 : cutoff], ("--remount-ro", "/"))

    def test_fixed_shell_alias_requires_exact_pinned_dash_and_recognized_host(self) -> None:
        with (
            patch.object(Path, "is_symlink", return_value=True),
            patch.object(Path, "resolve", return_value=Path("/usr/bin/dash")),
        ):
            self.assertEqual(
                namespace._system_shell_alias_args((Path("/usr/bin/dash"),), None),
                ["--symlink", "dash", "/usr/bin/sh"],
            )
            self.assertEqual(namespace._system_shell_alias_args((Path("/usr/bin/true"),), None), [])
            self.assertEqual(
                namespace._system_shell_alias_args((Path("/usr/bin/dash"), Path("/usr/bin")), None),
                [],
            )
            self.assertEqual(
                namespace._system_shell_alias_args((Path("/usr/bin/dash"),), Path("/usr/bin")),
                [],
            )
        for unsupported in ("/usr/bin/bash", "/opt/example-untrusted/sh"):
            with (
                self.subTest(target=unsupported),
                patch.object(Path, "is_symlink", return_value=True),
                patch.object(Path, "resolve", return_value=Path(unsupported)),
            ):
                self.assertEqual(
                    namespace._system_shell_alias_args(
                        (Path("/usr/bin/dash"), Path(unsupported)), None
                    ),
                    [],
                )
        with patch.object(Path, "is_symlink", return_value=False):
            self.assertEqual(namespace._system_shell_alias_args((Path("/usr/bin/dash"),), None), [])

    def test_project_cannot_cover_fixed_shell_alias_destination(self) -> None:
        for destination in ("/usr/bin", "/usr/bin/sh", "/usr/bin/sh/example"):
            with self.subTest(destination=destination), self.assertRaises(namespace.NamespaceError):
                namespace._validate_destinations(Path(destination), (Path("/usr/bin/true"),), None)

    def test_real_namespace_protects_aliases_and_retains_explicit_writable_mounts(self) -> None:
        executable = Path("/usr/bin/python3.12")
        roots = tuple(
            map(
                Path,
                (
                    str(executable),
                    "/usr/lib/python3.12",
                    "/usr/lib/x86_64-linux-gnu",
                    "/usr/lib64",
                    "/usr/bin/dash",
                ),
            )
        )
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        if not all(path.exists() for path in (*roots, bwrap)):
            namespace_unavailable(self, "system Python/dash runtime unavailable")
        require_namespace_runtime(self, bwrap)
        if Path("/usr/bin/sh").resolve() != Path("/usr/bin/dash"):
            namespace_unavailable(self, "recognized dash shell alias unavailable")
        try:
            runtime = namespace.NamespaceRuntime(bwrap, executable, roots)
            runtime.validate()
        except namespace.NamespaceError:
            namespace_unavailable(self, "system runtime is not immutable to this worker")
        (self.private / "key").write_text("fictional-authority", encoding="utf-8")
        code = r"""
import json, os, subprocess, sys
from pathlib import Path
project, private = map(Path, sys.argv[1:])
blocked = {}
actions = {
    "unlink_sh": lambda: os.unlink("/usr/bin/sh"),
    "rename_sh": lambda: os.rename("/usr/bin/sh", "/usr/bin/example-sh"),
    "replace_sh": lambda: os.replace("/lib", "/usr/bin/sh"),
    "create_bin_child": lambda: Path("/usr/bin/example-created").touch(),
    "chmod_bin": lambda: os.chmod("/usr/bin", 0o700),
    "rename_bin_parent": lambda: os.rename("/usr/bin", "/usr/example-bin"),
    "unlink_bin_alias": lambda: os.unlink("/bin"),
    "replace_bin_alias": lambda: os.replace("/lib", "/bin"),
    "runtime_write": lambda: Path("/usr/bin/dash").open("wb"),
    "git_write": lambda: (project / ".git/example-created").touch(),
}
for name, action in actions.items():
    try:
        action()
        blocked[name] = 0
    except OSError as error:
        blocked[name] = error.errno
writable = {}
for name, destination in (
    ("project", project / "example-created"),
    ("home", Path.home() / "example-created"),
    ("tmp", Path("/tmp/example-created")),
):
    try:
        destination.write_text("fictional-output")
        writable[name] = True
    except OSError:
        writable[name] = False
shell = subprocess.run(["/bin/sh", "-c", "printf example-shell"], capture_output=True, check=False)
print(json.dumps({"blocked": blocked, "writable": writable,
    "shell_ok": shell.returncode == 0 and shell.stdout == b"example-shell",
    "private_hidden": not private.exists()}))
"""
        for access in ("read-only", "read-write"):
            configured = dataclasses.replace(self.config, runtime=runtime, project_access=access)
            with (
                self.subTest(access=access),
                assert_descriptor_cleanup(self),
                configured.wrap(
                    [str(executable), "-I", "-c", code, str(self.project), str(self.private)],
                    {},
                    self.project,
                ) as launch,
            ):
                result = subprocess.run(
                    launch.argv,
                    env=launch.environment,
                    pass_fds=launch.pass_fds,
                    close_fds=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if result.returncode and namespace_permission_refused(result.stderr.decode()):
                    namespace_unavailable(self, "kernel refused the namespace fixture")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                report = json.loads(result.stdout)
                self.assertEqual(set(report["blocked"].values()), {errno.EROFS}, report)
                self.assertEqual(
                    report["writable"],
                    {"project": access == "read-write", "home": True, "tmp": True},
                )
                self.assertTrue(report["shell_ok"])
                self.assertTrue(report["private_hidden"])


if __name__ == "__main__":
    unittest.main()
