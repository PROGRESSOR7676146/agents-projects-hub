"""Offline filesystem and namespace tests for the guarded Claude file-tool slice."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.claude_file_sandbox as sandbox_module
from hermes_codex_router.claude_file_sandbox import (
    FileToolSandboxConfig,
    FileToolSandboxError,
    _reject_nested_mounts,
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
        cls.config = FileToolSandboxConfig(
            bwrap_executable=Path(shutil.which("bwrap") or "/usr/bin/true"),
            project_root=cls.project,
            provider_home=cls.home,
            runtime_roots=(Path("/usr/bin"),),
            claude_executable=cls.executable,
            python_executable=cls.executable,
            hook_code_root=Path("/usr/bin"),
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
        return self.config.wrap(
            [str(self.executable), "-c", code], {"LANG": "C.UTF-8"}, self.project
        )

    def test_exact_mounts_and_sanitized_environment(self) -> None:
        argv, env = self._wrap()
        self.assertIn("--unshare-user", argv)
        self.assertIn("--unshare-pid", argv)
        self.assertIn("--disable-userns", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("--proc", argv)
        self.assertIn("--remount-ro", argv)
        self.assertNotIn(str(self.private), argv)
        self.assertEqual(env["HOME"], "/home/example")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/example/.claude")
        self.assertNotIn("USER", env)
        self.assertEqual(argv[-3:], [str(self.executable), "-c", "pass"])

    def test_refuses_environment_and_path_authority_expansion(self) -> None:
        with self.assertRaises(FileToolSandboxError):
            self.config.wrap(
                [str(self.executable)],
                {"NODE_OPTIONS": "--require=/tmp/x"},
                self.project,
            )
        with self.assertRaises(FileToolSandboxError):
            self.config.wrap([str(self.executable)], {}, self.private)
        with self.assertRaises(FileToolSandboxError):
            self.config.wrap(["/usr/bin/bwrap"], {}, self.project)
        with self.assertRaises(FileToolSandboxError):
            self.config.wrap([str(self.executable)], {}, self.project / ".." / "project")

    def test_rejects_private_overlap_symlink_and_writable_hook(self) -> None:
        with patch("hermes_codex_router.claude_file_sandbox._immutable_tree"):
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
        with patch("hermes_codex_router.claude_file_sandbox._immutable_tree"):
            unsafe = dataclasses.replace(self.config, bwrap_executable=self.project / ".git")
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, self.project)
            unsafe = dataclasses.replace(self.config, runtime_roots=(self.project,))
            with self.assertRaises(FileToolSandboxError):
                unsafe.wrap([str(self.executable)], {}, self.project)

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

    def test_namespace_denies_private_symlink_git_write_and_host_paths(self) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bubblewrap runtime fixture unavailable")
        python_executable = Path("/usr/bin/python3.12")
        runtime = (
            Path("/usr/bin"),
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not python_executable.exists() or not all(path.exists() for path in runtime):
            self.skipTest("system Python runtime fixture unavailable")
        try:
            python_config = dataclasses.replace(
                self.config,
                claude_executable=python_executable,
                python_executable=python_executable,
                runtime_roots=runtime,
                hook_code_root=Path("/usr/lib/python3.12"),
            )
        except FileToolSandboxError:
            self.skipTest("system Python runtime is not immutable to this worker")
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
                "result['hook_write']=os.access('/usr/bin/true',os.W_OK); "
                "result['git_write']=os.access('.git',os.W_OK); "
                "host=pathlib.Path('/proc/" + str(os.getpid()) + "/cmdline'); "
                "result['host_pid_visible']=host.exists() and host.read_bytes()=="
                + repr(Path("/proc/self/cmdline").read_bytes())
                + "\n"
                "print(json.dumps(result))"
            )
            (self.project / "visible").write_text("visible", encoding="utf-8")
            argv, env = python_config.wrap(
                [str(python_executable), "-c", code], {"LANG": "C.UTF-8"}, self.project
            )
            run = subprocess.run(
                argv,
                env=env,
                cwd=self.project,
                close_fds=True,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if run.returncode and "Creating new namespace failed" in run.stderr:
                self.skipTest("kernel disallows user namespaces")
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["project"], "visible")
            self.assertFalse(result["private"])
            self.assertTrue(result["proc"])
            self.assertFalse(result["socket"])
            self.assertFalse(result["hook_write"])
            self.assertFalse(result["git_write"])
            self.assertFalse(result["host_pid_visible"])
        finally:
            host.close()
