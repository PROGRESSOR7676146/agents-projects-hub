"""Offline filesystem and namespace tests for the guarded Claude file-tool slice."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import hermes_codex_router.process_namespace as sandbox_module
from hermes_codex_router.claude_file_sandbox import (
    FileToolSandboxConfig,
    FileToolSandboxError,
)
from hermes_codex_router.process_namespace import _reject_nested_mounts
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)


class ClaudeFileSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="example-sandbox-")
        base = Path(cls.temp.name)
        cls.project = base / "project"
        cls.project.mkdir()
        (cls.project / ".git").mkdir()
        cls.home = base / "session"
        cls.home.mkdir(mode=0o700)
        cls.private = base / "authority"
        cls.private.mkdir(mode=0o700)
        (cls.private / "key").write_text("fictional-private-key", encoding="utf-8")
        cls.socket_path = base / "permission.sock"
        cls.sock = socket.socket(socket.AF_UNIX)
        cls.socket_emulated = False
        try:
            cls.sock.bind(str(cls.socket_path))
            cls.socket_path.chmod(0o600)
        except PermissionError:
            # Some CI sandboxes disallow AF_UNIX bind. The mount/path checks
            # remain testable with a disposable file; socket type is checked
            # by the production builder and in an ordinary Linux test host.
            cls.socket_emulated = True
            cls.socket_path.touch()
            cls.socket_path.chmod(0o600)
        cls.executable = Path("/usr/bin/true")
        # Builder tests need one immutable executable and a small readonly
        # directory, not every executable installed on the host. CI may have
        # /usr/bin symlinks into its provider-writable tool cache. The separate
        # namespace test below still validates its complete real runtime.
        cls.config = FileToolSandboxConfig(
            bwrap_executable=Path(shutil.which("bwrap") or "/usr/bin/true"),
            project_root=cls.project,
            provider_home=cls.home,
            runtime_roots=(cls.executable,),
            claude_executable=cls.executable,
            python_executable=cls.executable,
            hook_code_root=Path("/usr/lib/locale"),
            permission_socket=cls.socket_path,
            private_paths=(cls.private,),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sock.close()
        cls.temp.cleanup()

    def setUp(self) -> None:
        if self.socket_emulated:
            self.socket_patch = patch.object(sandbox_module.stat, "S_ISSOCK", return_value=True)
            self.socket_patch.start()

    def tearDown(self) -> None:
        if self.socket_emulated:
            self.socket_patch.stop()

    def _wrap(self, code: str = "pass") -> tuple[list[str], dict[str, str]]:
        with (
            patch.object(sandbox_module, "_require_fd_bind_support"),
            self.config.wrap(
                [str(self.executable), "-c", code], {"LANG": "C.UTF-8"}, self.project
            ) as launch,
        ):
            return list(launch.argv), launch.environment

    def test_exact_mounts_and_sanitized_environment(self) -> None:
        argv, env = self._wrap()
        self.assertIn("--unshare-user", argv)
        self.assertIn("--unshare-pid", argv)
        self.assertIn("--disable-userns", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("--proc", argv)
        self.assertIn("--remount-ro", argv)
        self.assertIn("--bind-fd", argv)
        self.assertIn("--ro-bind-fd", argv)
        self.assertNotIn("--bind", argv)
        self.assertNotIn("--ro-bind", argv)
        self.assertNotIn(str(self.private), argv)
        self.assertEqual(env["HOME"], "/home/example")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/example/.claude")
        self.assertNotIn("USER", env)
        self.assertEqual(argv[-3:], [str(self.executable), "-c", "pass"])

    def test_project_cannot_also_be_the_writable_session_home(self) -> None:
        # A second writable alias at HOME would expose .git without the
        # project's readonly overlay, even with different pinned descriptors.
        original_mode = stat.S_IMODE(self.project.stat().st_mode)
        self.project.chmod(0o700)
        before_fds = len(list(Path("/proc/self/fd").iterdir()))
        try:
            config = dataclasses.replace(self.config, provider_home=self.project)
            with (
                patch.object(sandbox_module, "_require_fd_bind_support"),
                self.assertRaisesRegex(FileToolSandboxError, "mount roles overlap"),
                config.wrap([str(self.executable)], {}, self.project),
            ):
                self.fail("equal project and session-home sources were accepted")
        finally:
            self.project.chmod(original_mode)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_distinct_home_path_cannot_alias_a_pinned_project_or_git_inode(self) -> None:
        # Model the descriptors of a top-level bind alias: names and mount IDs
        # can differ while fstat still identifies the same directory inode.
        # This exercises validation, not an actual host bind-mount operation.
        for target in (self.project, self.project / ".git"):
            with self.subTest(target=target.name):
                original_mode = stat.S_IMODE(target.stat().st_mode)
                target.chmod(0o700)
                try:
                    with sandbox_module.MountPins() as pins:
                        project_fd = pins.open(self.project, directory=True)
                        git_fd = pins.open_relative(project_fd, ".git", directory=True)
                        fds = {
                            self.project: project_fd,
                            self.project / ".git": git_fd,
                            self.home: project_fd if target == self.project else git_fd,
                            self.socket_path: pins.open(self.socket_path),
                            self.executable: pins.open(self.executable),
                            self.config.hook_code_root: pins.open(self.config.hook_code_root),
                        }
                        mount_ids = {path: pins.mount_id(fd) for path, fd in fds.items()}
                        mount_ids[self.home] = max(mount_ids.values()) + 1
                        self.assertNotEqual(mount_ids[self.home], mount_ids[target])
                        with self.assertRaisesRegex(FileToolSandboxError, "mount roles overlap"):
                            self.config._namespace._validate(mount_ids, fds)
                finally:
                    target.chmod(original_mode)

    def test_refuses_environment_and_path_authority_expansion(self) -> None:
        with (
            patch.object(sandbox_module, "_require_fd_bind_support"),
            self.assertRaisesRegex(FileToolSandboxError, "unapproved names"),
        ):
            self.config.wrap(
                [str(self.executable)],
                {"NODE_OPTIONS": "--require=/tmp/x"},
                self.project,
            )
        with (
            patch.object(sandbox_module, "_require_fd_bind_support"),
            self.assertRaisesRegex(FileToolSandboxError, "outside project"),
        ):
            self.config.wrap([str(self.executable)], {}, self.private)
        with (
            patch.object(sandbox_module, "_require_fd_bind_support"),
            self.assertRaisesRegex(FileToolSandboxError, "differs from pinned"),
        ):
            self.config.wrap(["/usr/bin/false"], {}, self.project)
        with (
            patch.object(sandbox_module, "_require_fd_bind_support"),
            self.assertRaisesRegex(FileToolSandboxError, "absolute canonical"),
        ):
            self.config.wrap([str(self.executable)], {}, self.project / ".." / "project")

    def test_home_cannot_alias_an_empty_or_populated_project_descendant(self) -> None:
        for relative, populated in (
            (".git/objects", False),
            ("ordinary", False),
            ("ordinary", True),
        ):
            with self.subTest(relative=relative, populated=populated):
                target = self.project / relative
                target.mkdir(mode=0o700)
                try:
                    if populated:
                        (target / "material").write_text("fictional material", encoding="utf-8")
                    with sandbox_module.MountPins() as pins:
                        project_fd = pins.open(self.project, directory=True)
                        fds = {
                            self.project: project_fd,
                            self.project / ".git": pins.open_relative(
                                project_fd, ".git", directory=True
                            ),
                            self.home: pins.open(target, directory=True),
                            self.socket_path: pins.open(self.socket_path),
                            self.executable: pins.open(self.executable),
                            self.config.hook_code_root: pins.open(self.config.hook_code_root),
                        }
                        before_fds = len(list(Path("/proc/self/fd").iterdir()))
                        with self.assertRaisesRegex(FileToolSandboxError, "mount roles overlap"):
                            self.config._namespace._validate(
                                {path: pins.mount_id(fd) for path, fd in fds.items()}, fds
                            )
                        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)
                finally:
                    shutil.rmtree(target)

    def test_home_symlink_to_project_is_not_a_writable_inode_alias(self) -> None:
        link = self.home / "project-link"
        link.symlink_to(self.project / ".git", target_is_directory=True)
        try:
            argv, _ = self._wrap()
            self.assertIn("--ro-bind-fd", argv)
        finally:
            link.unlink()

    def test_tree_identity_scan_includes_empty_root_and_ignores_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-tree-identities-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.mkdir()
            with sandbox_module.MountPins() as pins:
                fd = pins.open(root, directory=True)
                info = os.fstat(fd)
                self.assertEqual(
                    sandbox_module._scan_writable_tree(root, fd), {(info.st_dev, info.st_ino)}
                )
                (root / "link").symlink_to(outside, target_is_directory=True)
                self.assertEqual(
                    sandbox_module._scan_writable_tree(root, fd), {(info.st_dev, info.st_ino)}
                )

    def test_rejects_private_overlap_symlink_and_writable_hook(self) -> None:
        with patch("hermes_codex_router.process_namespace._immutable_tree"):
            unsafe = dataclasses.replace(self.config, private_paths=(self.project / ".git",))
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, self.project)
            unsafe = dataclasses.replace(self.config, hook_code_root=self.project)
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, self.project)
            link = self.project.parent / "link"
            link.symlink_to(self.project, target_is_directory=True)
            unsafe = dataclasses.replace(self.config, project_root=link)
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, link)

    def test_runtime_and_bwrap_must_be_immutable_to_worker(self) -> None:
        with patch("hermes_codex_router.process_namespace._immutable_tree"):
            unsafe = dataclasses.replace(self.config, bwrap_executable=self.project / ".git")
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, self.project)
            unsafe = dataclasses.replace(self.config, runtime_roots=(self.project,))
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, self.project)

    def test_other_uid_group_writes_and_acl_do_not_establish_trusted_code(self) -> None:
        info = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o644)
        with (
            patch.object(Path, "stat", return_value=info),
            patch.object(os, "access", return_value=False),
            patch.object(os, "listxattr", return_value=[]) as attributes,
        ):
            sandbox_module._immutable_source(self.executable, "runtime", directory=False)
            info.st_uid = os.geteuid() + 1
            with self.assertRaises(FileToolSandboxError):
                sandbox_module._immutable_source(self.executable, "runtime", directory=False)
            info.st_uid = 0
            info.st_mode = stat.S_IFREG | 0o664
            with self.assertRaises(FileToolSandboxError):
                sandbox_module._immutable_source(self.executable, "runtime", directory=False)
            info.st_mode = stat.S_IFREG | 0o644
            attributes.return_value = ["system.posix_acl_access"]
            with self.assertRaises(FileToolSandboxError):
                sandbox_module._immutable_source(self.executable, "runtime", directory=False)

    def test_writable_scan_checks_pinned_tree_even_if_named_tree_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-scan-pins-") as directory:
            root = Path(directory) / "project"
            root.mkdir()
            key = Path(directory) / "fictional-key"
            key.write_text("fictional", encoding="utf-8")
            os.link(key, root / "linked-key")
            with sandbox_module.MountPins() as pins:
                descriptor = pins.open(root, directory=True)
                root.rename(root.parent / "original")
                root.mkdir()  # Replacement has no hardlink; it must not be scanned instead.
                with self.assertRaisesRegex(FileToolSandboxError, "hardlink"):
                    sandbox_module._scan_writable_tree(root, descriptor)

    def test_unsupported_acl_storage_is_safe_but_inspection_failure_refuses(self) -> None:
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)
        with patch.object(os, "access", return_value=False):
            for error in (errno.ENOTSUP, errno.EOPNOTSUPP):
                with patch.object(os, "listxattr", side_effect=OSError(error, "example")):
                    sandbox_module._immutable_entry(
                        self.executable, "runtime", cast(os.stat_result, info)
                    )
            with patch.object(os, "listxattr", side_effect=OSError(errno.EACCES, "example")):
                with self.assertRaisesRegex(FileToolSandboxError, "cannot inspect runtime ACLs"):
                    sandbox_module._immutable_entry(
                        self.executable, "runtime", cast(os.stat_result, info)
                    )

    def test_wide_scan_under_low_fd_limit_and_failure_leave_no_handles(self) -> None:
        source = """
import json, os, resource, tempfile
from pathlib import Path
from hermes_codex_router.process_namespace import _scan_writable_tree, NamespaceError as FileToolSandboxError
from hermes_codex_router.claude_mount_pins import MountPins
with tempfile.TemporaryDirectory(prefix="example-wide-scan-") as directory:
    project = Path(directory) / "project"
    project.mkdir()
    for index in range(200):
        (project / str(index)).mkdir()
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, limits[0]), limits[1]))
    try:
        with MountPins() as pins:
            descriptor = pins.open(project, directory=True)
            count = len(os.listdir("/proc/self/fd"))
            _scan_writable_tree(project, descriptor)
            assert len(os.listdir("/proc/self/fd")) == count
            key = Path(directory) / "fictional-key"
            key.write_text("fictional")
            os.link(key, project / "199" / "linked-key")
            try:
                _scan_writable_tree(project, descriptor)
            except FileToolSandboxError as error:
                assert "hardlink" in str(error)
            else:
                raise AssertionError("hardlink accepted")
            assert len(os.listdir("/proc/self/fd")) == count
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, limits)
print(json.dumps({"wide_scan": "passed", "hardlink": "refused", "fds": "stable"}))
"""
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"wide_scan": "passed", "hardlink": "refused", "fds": "stable"},
        )

    def test_deep_scan_refuses_at_explicit_depth_bound_without_leaks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-deep-scan-") as directory:
            root = Path(directory)
            child = root
            for _ in range(12):
                child /= "d"
                child.mkdir()
            with sandbox_module.MountPins() as pins:
                descriptor = pins.open(root, directory=True)
                count = len(os.listdir("/proc/self/fd"))
                with patch.object(sandbox_module, "_MAX_SCAN_DEPTH", 8):
                    with self.assertRaisesRegex(FileToolSandboxError, "depth"):
                        sandbox_module._scan_writable_tree(root, descriptor)
                self.assertEqual(len(os.listdir("/proc/self/fd")), count)

    def test_scan_io_failures_are_bounded_and_close_ancestor_handles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-scan-errors-") as directory:
            root = Path(directory)
            (root / "entry").mkdir()
            actual_open = os.open
            with sandbox_module.MountPins() as pins:
                descriptor = pins.open(root, directory=True)
                for error, message in (
                    (errno.EACCES, "cannot inspect writable tree"),
                    (errno.ENOENT, "writable tree changed"),
                    (errno.EMFILE, "writable tree descriptor limit"),
                ):
                    count = len(os.listdir("/proc/self/fd"))

                    def fail_entry(path: str, flags: int, **kwargs: object) -> int:
                        if path == "entry":
                            raise OSError(error, "example")
                        return actual_open(path, flags, **kwargs)  # type: ignore[arg-type]

                    with patch.object(os, "open", side_effect=fail_entry):
                        with self.assertRaisesRegex(FileToolSandboxError, message):
                            sandbox_module._scan_writable_tree(root, descriptor)
                    self.assertEqual(len(os.listdir("/proc/self/fd")), count)

    def test_fdinfo_descriptor_exhaustion_has_scan_diagnostic_and_no_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-fdinfo-errors-") as directory:
            root = Path(directory)
            (root / "entry").mkdir()
            actual_read = Path.read_text
            reads = 0

            def fail_entry(path: Path, **kwargs: object) -> str:
                nonlocal reads
                if path.parent == Path("/proc/self/fdinfo"):
                    reads += 1
                    if reads > 1:
                        raise OSError(errno.EMFILE, "example")
                return actual_read(path, **kwargs)  # type: ignore[arg-type]

            with sandbox_module.MountPins() as pins:
                descriptor = pins.open(root, directory=True)
                count = len(os.listdir("/proc/self/fd"))
                with patch.object(Path, "read_text", fail_entry):
                    with self.assertRaisesRegex(
                        FileToolSandboxError, "writable tree descriptor limit"
                    ):
                        sandbox_module._scan_writable_tree(root, descriptor)
                self.assertEqual(len(os.listdir("/proc/self/fd")), count)

    def test_same_mount_with_different_device_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-subvolume-") as directory:
            root = Path(directory)
            (root / "entry").mkdir()
            actual_stat = os.fstat
            with sandbox_module.MountPins() as pins:
                descriptor = pins.open(root, directory=True)
                count = len(os.listdir("/proc/self/fd"))

                def subvolume(fd: int) -> object:
                    info = actual_stat(fd)
                    if fd == descriptor:
                        return info
                    return SimpleNamespace(
                        st_dev=info.st_dev + 1,
                        st_ino=info.st_ino,
                        st_mode=info.st_mode,
                        st_nlink=info.st_nlink,
                    )

                with patch.object(os, "fstat", side_effect=subvolume):
                    with self.assertRaisesRegex(FileToolSandboxError, "nested filesystem"):
                        sandbox_module._scan_writable_tree(root, descriptor)
                self.assertEqual(len(os.listdir("/proc/self/fd")), count)

    def test_preexisting_private_hardlink_is_rejected(self) -> None:
        link = self.project / "linked-authority"
        os.link(self.private / "key", link)
        try:
            with self.assertRaisesRegex(FileToolSandboxError, "hardlink"):
                self._wrap()
        finally:
            link.unlink()

    def test_nested_mount_is_rejected(self) -> None:
        mountinfo = f"1 2 0:1 / {self.project}/nested rw - tmpfs tmpfs rw\n"
        with patch.object(Path, "read_text", return_value=mountinfo):
            with self.assertRaisesRegex(FileToolSandboxError, "nested mount"):
                _reject_nested_mounts(self.project)

    def test_stacked_mount_uses_descriptor_identity_and_rejects_ambiguity(self) -> None:
        mountinfo = "1 2 0:1 / / rw - 9p example rw\n2 3 0:2 / / rw - ext4 example rw\n"
        with patch.object(Path, "read_text", return_value=mountinfo):
            with self.assertRaises(FileToolSandboxError):
                _reject_nested_mounts(self.project)
            _reject_nested_mounts(self.project, mount_ids={self.project: 2})
            with self.assertRaises(FileToolSandboxError):
                _reject_nested_mounts(self.project, mount_ids={self.project: 1})
            with self.assertRaises(FileToolSandboxError):
                _reject_nested_mounts(self.project, mount_ids={self.project: 3})

    def test_missing_descriptor_bind_capability_refuses_without_plain_fallback(self) -> None:
        with patch.object(
            sandbox_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"--bind SRC DEST\n", b""),
        ):
            with self.assertRaisesRegex(FileToolSandboxError, "descriptor binds"):
                sandbox_module._require_fd_bind_support(self.config.bwrap_executable)

    def test_source_replacement_during_validation_refuses_and_closes_pins(self) -> None:
        scan = sandbox_module._scan_writable_tree
        opened: list[int] = []
        pin_open = sandbox_module.MountPins.open
        original = self.home.parent / "example-original-home"

        def track(pins: sandbox_module.MountPins, *args: object, **kwargs: object) -> int:
            fd = pin_open(pins, *args, **kwargs)  # type: ignore[arg-type]
            opened.append(fd)
            return fd

        def replace(root: Path, source_fd: int) -> set[tuple[int, int]]:
            identities = scan(root, source_fd)
            if root == self.home:
                self.home.rename(original)
                self.home.mkdir(mode=0o700)
            return identities

        try:
            with (
                patch.object(sandbox_module.MountPins, "open", track),
                patch.object(sandbox_module, "_scan_writable_tree", side_effect=replace),
                self.assertRaises(FileToolSandboxError) as failure,
            ):
                self._wrap()
            self.assertIsInstance(failure.exception.__cause__, sandbox_module.MountPinError)
            self.assertIn("identity changed", str(failure.exception.__cause__))
            self.assertTrue(opened)
            for descriptor in set(opened):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        finally:
            if original.exists():
                self.home.rmdir()
                original.rename(self.home)

    def test_non_native_filesystems_and_broad_home_roots_are_refused(self) -> None:
        for filesystem in ("9p", "drvfs", "fuse", "ntfs", "vfat", "unknown"):
            mountinfo = f"1 2 0:1 / / rw - {filesystem} example rw\n"
            with patch.object(Path, "read_text", return_value=mountinfo):
                with self.subTest(filesystem=filesystem), self.assertRaises(FileToolSandboxError):
                    _reject_nested_mounts(self.project)
        mountinfo = "1 2 0:1 / / rw - ext4 example rw\n"
        with patch.object(Path, "read_text", return_value=mountinfo):
            _reject_nested_mounts(self.project)
        with self.assertRaises(FileToolSandboxError):
            sandbox_module._not_broad(Path("/home/example"), "project root")

    def test_namespace_denies_private_symlink_git_write_and_host_paths(self) -> None:
        if shutil.which("bwrap") is None:
            namespace_unavailable(self, "bubblewrap runtime fixture unavailable")
        require_namespace_runtime(self, self.config.bwrap_executable)
        python_executable = Path("/usr/bin/python3.12")
        runtime = (
            python_executable,
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not python_executable.exists() or not all(path.exists() for path in runtime):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        try:
            python_config = dataclasses.replace(
                self.config,
                claude_executable=python_executable,
                python_executable=python_executable,
                runtime_roots=runtime,
                hook_code_root=Path("/usr/lib/python3.12"),
            )
        except FileToolSandboxError:
            namespace_unavailable(self, "system Python runtime is not immutable to this worker")
        (self.project / "escape").symlink_to(self.private / "key")
        host_socket = self.private / "host.sock"
        host = socket.socket(socket.AF_UNIX)
        try:
            host.bind(str(host_socket))
        except PermissionError:
            host_socket.touch()
        try:
            code = (
                "import os,socket,pathlib,json; "
                "p=pathlib.Path('.'); result={}; "
                "result['project']=p.joinpath('visible').read_text(); "
                "result['private']=p.joinpath('escape').exists(); "
                "result['proc']=pathlib.Path('/proc/self/environ').exists(); "
                "result['socket']=pathlib.Path('" + str(host_socket) + "').exists(); "
                "result['hook_write']=os.access('/usr/bin/python3.12',os.W_OK); "
                "result['git_write']=os.access('.git',os.W_OK); "
                "result['pid1_visible']=pathlib.Path('/proc/1').is_dir(); "
                "host=pathlib.Path('/proc/" + str(os.getpid()) + "/cmdline'); "
                "result['host_pid_visible']=host.exists() and host.read_bytes()=="
                + repr(Path("/proc/self/cmdline").read_bytes())
                + "\n"
                "\n"
                "result['inherited_pins']=[]\n"
                "result['fd_tables']={}\n"
                "for process in pathlib.Path('/proc').iterdir():\n"
                " if not process.name.isdigit(): continue\n"
                " try:\n"
                "  descriptors=list(process.joinpath('fd').iterdir())\n"
                " except PermissionError:\n"
                "  result['fd_tables'][process.name]='inaccessible'\n"
                "  continue\n"
                " except FileNotFoundError: continue\n"
                " result['fd_tables'][process.name]='readable'\n"
                " for descriptor in descriptors:\n"
                "  try:\n"
                "   info=descriptor.stat()\n"
                "   if (info.st_dev,info.st_ino) in EXPECTED_PINS:\n"
                "    result['inherited_pins'].append(str(descriptor))\n"
                "  except (PermissionError,FileNotFoundError): pass\n"
                "result['self_pid']=str(os.getpid())\n"
                "print(json.dumps(result))"
            )
            (self.project / "visible").write_text("visible", encoding="utf-8")
            with python_config.wrap(
                [str(python_executable), "-c", code], {"LANG": "C.UTF-8"}, self.project
            ) as launch:
                identities = {(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in launch.pass_fds}
                argv = list(launch.argv)
                argv[-1] = argv[-1].replace("EXPECTED_PINS", repr(identities))
                original = self.project.parent / "example-original-project"
                self.project.rename(original)
                self.project.symlink_to(self.private, target_is_directory=True)
                try:
                    run = subprocess.run(
                        argv,
                        env=launch.environment,
                        cwd="/",
                        close_fds=True,
                        pass_fds=launch.pass_fds,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                finally:
                    self.project.unlink()
                    original.rename(self.project)
            if run.returncode and namespace_permission_refused(run.stderr):
                namespace_unavailable(self, "kernel disallows user namespaces")
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["project"], "visible")
            self.assertFalse(result["private"])
            self.assertTrue(result["proc"])
            self.assertFalse(result["socket"])
            self.assertFalse(result["hook_write"])
            self.assertFalse(result["git_write"])
            self.assertFalse(result["host_pid_visible"])
            self.assertTrue(result["pid1_visible"])
            self.assertIn(result["fd_tables"]["1"], ("readable", "inaccessible"))
            self.assertEqual(result["fd_tables"][result["self_pid"]], "readable")
            self.assertEqual(result["inherited_pins"], [])
        finally:
            host.close()
