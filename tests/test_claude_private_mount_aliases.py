"""Private authority stays absent even when mount names describe an alias."""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import hermes_codex_router.claude_file_sandbox as sandbox
import hermes_codex_router.claude_mount_pins as mounts
import hermes_codex_router.claude_private_mounts as private_mounts
import hermes_codex_router.process_namespace as namespace
from tests.fd_fixture import assert_descriptor_cleanup
from tests.namespace_fixture import namespace_permission_refused, namespace_unavailable


class ClaudePrivateMountAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="example-private-mounts-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "lane" / "project"
        self.project.mkdir(parents=True)
        (self.project / ".git").mkdir()
        self.home = self.base / "session"
        self.home.mkdir(mode=0o700)
        self.private = self.base / "authority"
        self.private.mkdir()
        (self.private / "key").write_text("fictional-key", encoding="utf-8")
        self.socket_path = self.base / "permission.sock"
        self.sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.sock.close)
        try:
            self.sock.bind(str(self.socket_path))
        except PermissionError:
            self.socket_path.touch()
            self.socket_patch = patch.object(namespace.stat, "S_ISSOCK", return_value=True)
            self.socket_patch.start()
            self.addCleanup(self.socket_patch.stop)
        self.socket_path.chmod(0o600)
        runtime = self.base / "runtime"
        runtime.mkdir()
        self.executable = runtime / "example-executable"
        self.executable.write_text("fictional executable", encoding="utf-8")
        self.executable.chmod(0o755)
        hook = self.base / "hooks"
        hook.mkdir()
        # Alias metadata needs all fixture sources on one filesystem. Runtime
        # trust has separate tests; bypass only these local fictional sources.
        for name in ("_immutable_source", "_immutable_entry", "_runtime_location"):
            original = getattr(namespace, name)

            def fixture_source(path: Path, *args: object, original=original, **kwargs: object):
                if path == self.base or self.base in path.parents:
                    return None
                return original(path, *args, **kwargs)

            scoped = patch.object(namespace, name, fixture_source)
            scoped.start()
            self.addCleanup(scoped.stop)
        self.config = sandbox.FileToolSandboxConfig(
            bwrap_executable=Path(shutil.which("bwrap") or "/usr/bin/true"),
            project_root=self.project,
            provider_home=self.home,
            runtime_roots=(self.executable,),
            claude_executable=self.executable,
            python_executable=self.executable,
            hook_code_root=hook,
            permission_socket=self.socket_path,
            private_paths=(self.private,),
        )

    @contextmanager
    def alias(self, destination: Path, filesystem_root: Path) -> Iterator[None]:
        """Inject only kernel mount metadata, leaving actual fixture inodes intact."""
        original_read = Path.read_text
        original_id = mounts.mount_id
        table = Path("/proc/self/mountinfo").read_bytes().decode("utf-8")
        identity = max(int(line.split(" ")[0]) for line in table.removesuffix("\n").split("\n")) + 1
        device = destination.stat().st_dev
        anchor = filesystem_root
        while not anchor.exists():
            anchor = anchor.parent
        self.assertEqual(device, anchor.stat().st_dev, "alias fixture must model origin device")
        with mounts.MountPins() as pins:
            origin_fd = pins.open(anchor)
            origin_id = pins.mount_id(origin_fd)
        selected_mount = next(
            line.split(" ")
            for line in table.removesuffix("\n").split("\n")
            if line.split(" ")[0] == str(origin_id)
        )

        def decode(raw: str) -> Path:
            return Path(re.sub(r"\\(040|011|012|134)", lambda match: chr(int(match[1], 8)), raw))

        def encode(path: Path) -> str:
            return (
                str(path)
                .replace("\\", r"\134")
                .replace(" ", r"\040")
                .replace("\t", r"\011")
                .replace("\n", r"\012")
            )

        coordinate = decode(selected_mount[3]) / filesystem_root.relative_to(
            decode(selected_mount[4])
        )
        table += (
            f"{identity} 1 {os.major(device)}:{os.minor(device)} "
            f"{encode(coordinate)} {encode(destination)} rw - ext4 example rw\n"
        )

        def read(path: Path, *args: object, **kwargs: object) -> str:
            if path == Path("/proc/self/mountinfo"):
                return table
            return original_read(path, *args, **kwargs)  # type: ignore[arg-type]

        def selected(fd: int) -> int:
            name = Path(os.readlink(f"/proc/self/fd/{fd}"))
            return (
                identity if name == destination or destination in name.parents else original_id(fd)
            )

        with (
            patch.object(Path, "read_text", read),
            patch.object(private_mounts, "_read_mountinfo", return_value=table),
            patch.object(mounts, "mount_id", selected),
            patch.object(namespace, "mount_id", selected),
        ):
            yield

    def wrap(self, config: sandbox.FileToolSandboxConfig | None = None) -> mounts.SandboxLaunch:
        with patch.object(namespace, "_require_fd_bind_support"):
            return self.launch(config)

    def launch(self, config: sandbox.FileToolSandboxConfig | None = None) -> mounts.SandboxLaunch:
        return (config or self.config).wrap([str(self.executable)], {}, self.project)

    @contextmanager
    def assert_opened_descriptors_closed(self) -> Iterator[None]:
        with assert_descriptor_cleanup(self):
            yield

    def test_project_root_ancestor_and_subtree_private_aliases_are_refused(self) -> None:
        for destination, origin in (
            (self.project, self.private),
            (self.project.parent, self.private),
            (self.project, self.private / "subtree"),
            (self.project, self.private.parent),
        ):
            with self.subTest(destination=destination, origin=origin):
                with self.assert_opened_descriptors_closed(), self.alias(destination, origin):
                    with self.assertRaisesRegex(
                        sandbox.FileToolSandboxError, "mount overlaps private authority"
                    ):
                        with self.wrap():
                            self.fail("private filesystem coordinate was exposed")

    def test_home_readonly_runtime_and_hook_private_aliases_are_refused(self) -> None:
        for destination, origin in (
            (self.home, self.private / "sessions"),
            (self.executable, self.private / "key"),
            (self.config.hook_code_root, self.private),
        ):
            with self.subTest(destination=destination), self.alias(destination, origin):
                with self.assertRaisesRegex(
                    sandbox.FileToolSandboxError, "mount overlaps private authority"
                ):
                    with self.wrap():
                        self.fail("readonly or home alias disclosed authority")

    def test_same_filesystem_siblings_and_private_validation_fds_are_safe(self) -> None:
        with self.assert_opened_descriptors_closed(), self.wrap() as launch:
            private_info = self.private.stat()
            public_sources = {
                str(path)
                for path in (
                    self.project,
                    self.project / ".git",
                    self.home,
                    self.executable,
                    self.config.hook_code_root,
                    self.socket_path,
                )
            }
            self.assertTrue(launch.pass_fds)
            for fd in launch.pass_fds:
                self.assertIn(os.readlink(f"/proc/self/fd/{fd}"), public_sources)
                info = os.fstat(fd)
                self.assertNotEqual(
                    (info.st_dev, info.st_ino), (private_info.st_dev, private_info.st_ino)
                )
            self.assertNotIn(str(self.private), launch.argv)

    def test_protected_socket_is_not_the_per_turn_permission_exception(self) -> None:
        config = dataclasses.replace(self.config, private_paths=(self.socket_path,))
        with self.assertRaisesRegex(
            sandbox.FileToolSandboxError, "mount overlaps private authority"
        ):
            with self.wrap(config):
                self.fail("a protected socket was exposed as the per-turn endpoint")

    def test_permission_socket_bind_alias_of_private_endpoint_is_refused(self) -> None:
        with self.alias(self.socket_path, self.private / "endpoint"):
            with self.assertRaisesRegex(
                sandbox.FileToolSandboxError, "mount overlaps private authority"
            ):
                with self.wrap():
                    self.fail("private endpoint coordinate was exposed")

    def test_hardlinked_permission_socket_is_refused(self) -> None:
        os.link(self.socket_path, self.private / "endpoint")
        with self.assert_opened_descriptors_closed():
            with self.assertRaisesRegex(
                sandbox.FileToolSandboxError, "permission socket.*hardlink"
            ):
                with self.wrap():
                    self.fail("hardlinked private endpoint was exposed")

    def test_missing_private_suffix_does_not_protect_its_whole_existing_parent(self) -> None:
        config = dataclasses.replace(
            self.config, private_paths=(self.base / "future" / "authority",)
        )
        with self.wrap(config):
            pass
        with self.alias(self.project, self.base / "future" / "authority" / "subtree"):
            with self.assertRaisesRegex(
                sandbox.FileToolSandboxError, "mount overlaps private authority"
            ):
                with self.wrap(config):
                    self.fail("alias of missing authority suffix was exposed")

    def test_private_mount_on_another_device_cannot_hide_below_its_root(self) -> None:
        original = Path.read_text
        table = Path("/proc/self/mountinfo").read_bytes().decode("utf-8")
        identity = max(int(line.split(" ")[0]) for line in table.removesuffix("\n").split("\n")) + 1
        table += f"{identity} 1 0:999 /secret {self.private}/nested rw - tmpfs example rw\n"

        def read(path: Path, *args: object, **kwargs: object) -> str:
            if path == Path("/proc/self/mountinfo"):
                return table
            return original(path, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(Path, "read_text", read),
            patch.object(private_mounts, "_read_mountinfo", return_value=table),
        ):
            with self.assertRaisesRegex(sandbox.FileToolSandboxError, "private authority"):
                with self.wrap():
                    self.fail("private descendant mount was not accounted for")

    def test_private_path_appearing_before_launch_is_refused_without_fd_leaks(self) -> None:
        future = self.base / "future"
        config = dataclasses.replace(self.config, private_paths=(future / "authority",))

        def appear(_: Path) -> None:
            future.mkdir()

        with (
            self.assert_opened_descriptors_closed(),
            patch.object(namespace, "_require_fd_bind_support", side_effect=appear),
        ):
            with self.assertRaisesRegex(sandbox.FileToolSandboxError, "private authority"):
                with self.launch(config):
                    self.fail("a missing private component changed after validation")

    def test_private_ancestor_replacement_cannot_change_the_excluded_inode(self) -> None:
        original = self.base / "original-authority"

        def replace(_: Path) -> None:
            self.private.rename(original)
            self.private.mkdir()

        with (
            self.assert_opened_descriptors_closed(),
            patch.object(namespace, "_require_fd_bind_support", side_effect=replace),
        ):
            with self.assertRaises(sandbox.FileToolSandboxError):
                with self.launch():
                    self.fail("private path replacement escaped its pinned identity")

    def test_independent_bind_coordinate_on_same_filesystem_is_allowed(self) -> None:
        with self.alias(self.project, self.base / "independent"):
            with self.wrap():
                pass

    def test_scripted_bind_coordinates_preserve_kernel_path_escapes(self) -> None:
        for name in ("space here", "back\\slash", "tab\tname", "new\nline"):
            with self.subTest(name=name):
                with self.alias(self.project, self.base / name):
                    with self.wrap():
                        pass
                with self.alias(self.project, self.private / name):
                    with self.assertRaisesRegex(
                        sandbox.FileToolSandboxError, "mount overlaps private authority"
                    ):
                        with self.wrap():
                            self.fail("escaped private coordinate was not excluded")

    def test_unescaped_bind_roots_preserve_private_containment(self) -> None:
        for character in ("\u00a0", "\u3000", "\x1f", "\r", "\u2028", "\v", "\f", "\x85"):
            with self.subTest(character=repr(character)):
                with self.alias(self.project, self.base / f"sibling{character}/subtree"):
                    with self.wrap():
                        pass
                with self.alias(self.project, self.private / f"child{character}/subtree"):
                    with self.assertRaisesRegex(
                        sandbox.FileToolSandboxError, "mount overlaps private authority"
                    ):
                        with self.wrap():
                            self.fail("unescaped private subtree coordinate was not excluded")

    def test_real_private_bind_alias_is_refused_before_any_provider_launch(self) -> None:
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            namespace_unavailable(self, "bubblewrap runtime fixture unavailable")
        (self.private / ".git").mkdir()
        (self.private / "subtree").mkdir()
        (self.private / "subtree" / ".git").mkdir()
        script = """
import sys
from pathlib import Path
from hermes_codex_router.claude_mount_pins import MountPins
from hermes_codex_router.claude_private_mounts import PrivateMountGuard, PrivateMountError
base=Path(sys.argv[1]); project=base/'lane/project'
try:
    with MountPins() as pins, PrivateMountGuard() as guard:
        fd=pins.open(project, directory=True)
        guard.check({project:fd}, {project:pins.mount_id(fd)}, (base/'authority',))
        raise AssertionError('real private bind alias accepted')
except PrivateMountError as error:
    assert str(error)=='mount overlaps private authority', str(error)
print('real private bind alias refused')
"""
        for origin, destination in (
            (self.private, self.project),
            (self.private / "subtree", self.project),
            (self.private, self.project.parent),
        ):
            with self.subTest(origin=origin, destination=destination):
                if destination == self.project.parent:
                    (self.private / "project").mkdir()
                    (self.private / "project" / ".git").mkdir()
                result = subprocess.run(
                    [
                        bwrap,
                        "--unshare-user",
                        "--unshare-pid",
                        "--die-with-parent",
                        "--ro-bind",
                        "/",
                        "/",
                        "--dev",
                        "/dev",
                        "--proc",
                        "/proc",
                        "--bind",
                        str(self.base),
                        str(self.base),
                        "--bind",
                        str(origin),
                        str(destination),
                        "--",
                        sys.executable,
                        "-c",
                        script,
                        str(self.base),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if result.returncode and namespace_permission_refused(result.stderr):
                    namespace_unavailable(self, "kernel disallows user namespaces")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "real private bind alias refused")


class ReadonlyPrivateMountAliasTests(ClaudePrivateMountAliasTests):
    """The same provenance corpus through the direct core, bypassing Claude.wrap."""

    def launch(self, config: sandbox.FileToolSandboxConfig | None = None) -> mounts.SandboxLaunch:
        selected = config or self.config
        core = namespace.ProcessNamespaceConfig(
            runtime=selected._namespace.runtime,
            project_root=selected.project_root,
            session_home=selected.provider_home,
            private_paths=selected.private_paths,
        )
        return core.wrap([str(self.executable)], {}, self.project)

    def assert_no_endpoint(self, config: sandbox.FileToolSandboxConfig | None = None) -> None:
        with self.assert_opened_descriptors_closed(), self.wrap(config) as launch:
            self.assertNotIn("--share-net", launch.argv)
            self.assertNotIn("/run/hub-permission.sock", launch.argv)
            for fd in launch.pass_fds:
                self.assertNotEqual(os.readlink(f"/proc/self/fd/{fd}"), str(self.socket_path))

    def test_protected_socket_is_not_the_per_turn_permission_exception(self) -> None:
        self.assert_no_endpoint(dataclasses.replace(self.config, private_paths=(self.socket_path,)))

    def test_permission_socket_bind_alias_of_private_endpoint_is_refused(self) -> None:
        with self.alias(self.socket_path, self.private / "endpoint"):
            self.assert_no_endpoint()

    def test_hardlinked_permission_socket_is_refused(self) -> None:
        os.link(self.socket_path, self.private / "endpoint")
        self.assert_no_endpoint()
