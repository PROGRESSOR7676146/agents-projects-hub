"""A second, provider-free consumer of the guarded process namespace."""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.process_namespace as namespace
from hermes_codex_router.process_namespace import (
    NamespaceError,
    NamespaceRuntime,
    ProcessNamespaceConfig,
)


class ProcessNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-process-namespace-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.home = self.base / "session"
        self.private = self.base / "authority"
        for path in (self.project, self.home, self.private):
            path.mkdir(mode=0o700)
        (self.project / ".git").mkdir()
        self.runtime = NamespaceRuntime(
            bwrap_executable=Path(shutil.which("bwrap") or "/usr/bin/true"),
            executable=Path("/usr/bin/true"),
            runtime_roots=(Path("/usr/bin/true"),),
        )
        self.config = ProcessNamespaceConfig(
            runtime=self.runtime,
            project_root=self.project,
            session_home=self.home,
            private_paths=(self.private,),
        )

    def test_default_launch_reads_project_without_shared_network_or_socket(self) -> None:
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            patch.dict(os.environ, {"EXAMPLE_AMBIENT_SECRET": "fictional"}),
            self.config.wrap(
                ["/usr/bin/true"], {"HOME": "/wrong", "LANG": "C"}, self.project
            ) as launch,
        ):
            self.assertNotIn("--share-net", launch.argv)
            self.assertNotIn("--bind-fd", launch.argv[: launch.argv.index("--ro-bind-fd")])
            project_destination = launch.argv.index(str(self.project))
            self.assertEqual(launch.argv[project_destination - 2], "--ro-bind-fd")
            home_destination = launch.argv.index("/home/example")
            self.assertEqual(launch.argv[home_destination - 2], "--bind-fd")
            self.assertNotIn("/run/hub-permission.sock", launch.argv)
            self.assertEqual(launch.environment["HOME"], "/home/example")
            self.assertNotIn("CLAUDE_CONFIG_DIR", launch.environment)
            self.assertNotIn("EXAMPLE_AMBIENT_SECRET", launch.environment)
            self.assertEqual(launch.environment["LANG"], "C")
            self.assertTrue(launch.pass_fds)

    def test_explicit_write_and_shared_network_require_no_alternate_builder(self) -> None:
        configured = dataclasses.replace(self.config, project_access="read-write", network="shared")
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            configured.wrap(["/usr/bin/true"], {}, self.project) as launch,
        ):
            destination = launch.argv.index(str(self.project))
            self.assertEqual(launch.argv[destination - 2], "--bind-fd")
            self.assertIn("--share-net", launch.argv)
            git_destination = launch.argv.index(str(self.project / ".git"))
            self.assertEqual(launch.argv[git_destination - 2], "--ro-bind-fd")

    def test_invalid_access_modes_refuse_instead_of_selecting_a_default(self) -> None:
        for field, values in (
            ("project_access", ("write", "", None)),
            ("network", ("host", "", None)),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(NamespaceError):
                    dataclasses.replace(self.config, **{field: value})

    def test_private_network_profile_refuses_a_host_permission_endpoint(self) -> None:
        with self.assertRaisesRegex(NamespaceError, "private network.*permission socket"):
            dataclasses.replace(self.config, permission_socket=self.base / "permission.sock")

    def test_wrap_rechecks_the_original_runtime_identity_and_closes_descriptors(self) -> None:
        before = len(list(Path("/proc/self/fd").iterdir()))
        with (
            patch.object(NamespaceRuntime, "_runtime_identity", return_value=(0, 0, 0, 0)),
            self.assertRaisesRegex(NamespaceError, "trusted runtime identity changed"),
        ):
            self.config.wrap(["/usr/bin/true"], {}, self.project)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)

    def test_project_destination_cannot_overlap_namespace_owned_paths(self) -> None:
        for destination in (
            "/home/example/project",
            "/home/example",
            "/proc/project",
            "/dev/project",
            "/run/project",
            "/tmp",
        ):
            with (
                self.subTest(destination=destination),
                self.assertRaisesRegex(NamespaceError, "destination"),
            ):
                namespace._validate_destinations(
                    Path(destination), self.runtime.runtime_roots, None
                )
        namespace._validate_destinations(self.project, self.runtime.runtime_roots, None)


if __name__ == "__main__":
    unittest.main()
