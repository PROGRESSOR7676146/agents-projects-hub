from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
from hermes_codex_router.process_namespace import (
    NamespaceError,
    NamespaceRuntime,
    ProcessNamespaceConfig,
)
from hermes_codex_router.runtime_release import (
    UserRuntimeRelease,
    parse_user_releases,
    release_digest,
    seal_release,
)
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_mapped_host_root,
    require_namespace_runtime,
)


def setUpModule() -> None:
    require_mapped_host_root()


class UserRuntimeReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-user-release-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.release = self.base / "releases/version-one"
        (self.release / "bin").mkdir(parents=True)
        (self.release / "hooks").mkdir()
        shutil.copyfile("/usr/bin/true", self.release / "bin/true")
        (self.release / "bin/true").chmod(0o555)
        (self.release / "hooks/control.py").write_text("EXAMPLE = 'original'\n")
        (self.release / "hooks/control.py").chmod(0o444)
        for path in (self.release / "bin", self.release / "hooks", self.release):
            path.chmod(0o555)

    def pin(self) -> UserRuntimeRelease:
        return UserRuntimeRelease(self.release, release_digest(self.release))

    def runtime(self, pin: UserRuntimeRelease | None = None) -> NamespaceRuntime:
        return NamespaceRuntime(
            Path("/usr/bin/true"),
            self.release / "bin/true",
            (self.release,),
            code_root=self.release / "hooks",
            user_releases=() if pin is None else (pin,),
        )

    def test_explicit_owned_release_passes_and_legacy_default_stays_strict(self) -> None:
        with self.assertRaises(NamespaceError):
            self.runtime()
        runtime = self.runtime(self.pin())
        self.assertEqual(runtime.validate()[1], (self.release,))

    def test_config_requires_bounded_exact_canonical_pins(self) -> None:
        raw = {"root": str(self.release), "sha256": self.pin().sha256}
        self.assertEqual(parse_user_releases([raw]), (self.pin(),))
        for invalid in (
            [],
            [raw, raw],
            [raw] * 9,
            [{**raw, "sha256": True}],
            [{**raw, "sha256": "wrong"}],
            [{**raw, "root": "/home/example/../release"}],
            [{**raw, "automatic": True}],
            [{"root": str(self.release)}],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_user_releases(invalid)

    def test_changed_content_is_refused_even_when_source_root_identity_is_unchanged(self) -> None:
        pin = self.pin()
        runtime = self.runtime(pin)
        original = runtime._runtime_identity(self.release)
        code = self.release / "hooks/control.py"
        code.chmod(0o644)
        code.write_text("EXAMPLE = 'replaced'\n")
        code.chmod(0o444)
        self.assertEqual(runtime._runtime_identity(self.release), original)
        with self.assertRaisesRegex(NamespaceError, "release"):
            runtime.validate()

    def test_wrong_digest_and_writable_descendants_are_refused(self) -> None:
        with self.assertRaises(NamespaceError):
            self.runtime(UserRuntimeRelease(self.release, "0" * 64))
        pin = self.pin()
        (self.release / "hooks/control.py").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "sealed read-only"):
            release_digest(self.release)
        with self.assertRaises(NamespaceError):
            self.runtime(pin)

    def test_unsafe_ancestors_acls_and_special_entries_are_refused(self) -> None:
        self.release.parent.chmod(0o775)
        with self.assertRaisesRegex(ValueError, "ancestor"):
            self.pin()
        self.release.parent.chmod(0o755)
        with patch(
            "hermes_codex_router.runtime_release.os.listxattr",
            return_value=["system.posix_acl_access"],
        ):
            with self.assertRaisesRegex(ValueError, "ACL"):
                self.pin()
        self.release.chmod(0o755)
        os.mkfifo(self.release / "special", 0o400)
        self.release.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "special file"):
            self.pin()

    def test_pin_does_not_authorize_user_owned_bwrap_or_unregistered_code(self) -> None:
        runtime = self.runtime(self.pin())
        with self.assertRaises(NamespaceError):
            dataclasses.replace(runtime, bwrap_executable=self.release / "bin/true").validate()
        other = self.base / "other"
        other.mkdir()
        with self.assertRaises(NamespaceError):
            dataclasses.replace(runtime, code_root=other)

    def test_symbolic_root_and_hardlinked_code_are_refused(self) -> None:
        alias = self.base / "alias"
        alias.symlink_to(self.release, target_is_directory=True)
        with self.assertRaises(ValueError):
            UserRuntimeRelease(alias, self.pin().sha256).verify()
        code = self.release / "hooks/control.py"
        os.link(code, self.base / "alias.py")
        with self.assertRaises(ValueError):
            release_digest(self.release)

    def updater(self) -> Any:
        path = Path(__file__).resolve().parents[1] / "scripts/stack.py"
        spec = importlib.util.spec_from_file_location("example_stack", path)
        assert spec and spec.loader
        module: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_updater_and_launch_policy_use_the_same_tree_digest(self) -> None:
        module = self.updater()
        self.release.chmod(0o755)
        (self.release / "hooks").chmod(0o755)
        for name, target in (
            ("dot-link", "./hooks/control.py"),
            ("slash-link", "hooks//control.py"),
            ("directory-link", "hooks/"),
            ("absolute-link", str(self.release / "bin/true")),
            ("hooks/parent-link", "../bin/true"),
        ):
            (self.release / name).symlink_to(target)
        (self.release / "hooks").chmod(0o555)
        self.release.chmod(0o555)
        self.assertEqual(module.tree_digest(self.release), release_digest(self.release))

    def test_outside_current_link_is_refused_even_when_it_resolves_inside_release(self) -> None:
        module = self.updater()
        current = self.base / "current"
        current.symlink_to(self.release, target_is_directory=True)
        self.release.chmod(0o755)
        nested = self.release / "nested/nested"
        nested.mkdir(parents=True)
        (nested / "os").symlink_to("/usr/bin", target_is_directory=True)
        nested.chmod(0o555)
        nested.parent.chmod(0o555)
        self.release.chmod(0o555)
        link = self.release / "alias"
        for target in (
            str(current / "bin/true"),
            "../../current/bin/true",
            "/usr/.." + str(current / "bin/true"),
            "nested/nested/os/../../../" + str(current / "bin/true").lstrip("/"),
        ):
            with self.subTest(target=target):
                self.release.chmod(0o755)
                link.symlink_to(target)
                self.release.chmod(0o555)
                try:
                    self.assertEqual(link.resolve(strict=True), self.release / "bin/true")
                    for inspect in (module.tree_digest, release_digest, seal_release):
                        with self.subTest(inspect=inspect.__name__), self.assertRaises(ValueError):
                            inspect(self.release)
                finally:
                    self.release.chmod(0o755)
                    link.unlink()
                    self.release.chmod(0o555)

    def test_unreadable_directory_cannot_be_omitted_from_either_digest(self) -> None:
        module = self.updater()
        directory = self.release / "hooks"
        directory.chmod(0o111)
        try:
            for inspect in (module.tree_digest, release_digest):
                with self.subTest(inspect=inspect.__name__), self.assertRaises(PermissionError):
                    inspect(self.release)
        finally:
            directory.chmod(0o555)

    def test_sealing_owned_version_and_symlink_validation(self) -> None:
        self.release.chmod(0o755)
        code = self.release / "hooks/control.py"
        code.chmod(0o644)
        (self.release / "internal").symlink_to("hooks/control.py")
        (self.release / "system").symlink_to("/usr/bin/true")
        pin = seal_release(self.release)
        pin.verify()
        self.assertEqual(code.stat().st_mode & 0o222, 0)
        self.assertEqual(self.release.stat().st_mode & 0o222, 0)
        self.release.chmod(0o755)
        (self.release / "escape").symlink_to("/usr/../etc/passwd")
        with self.assertRaisesRegex(ValueError, "escapes runtime"):
            seal_release(self.release)

    def test_claude_sandbox_propagates_pins_to_launch_validation(self) -> None:
        config = FileToolSandboxConfig(
            Path("/usr/bin/true"),
            self.base / "project",
            self.base / "session",
            (self.release,),
            self.release / "bin/true",
            self.release / "bin/true",
            self.release / "hooks",
            self.base / "permission.sock",
            (self.base / "authority",),
            user_releases=(self.pin(),),
        )
        config._namespace.runtime.validate()
        code = self.release / "hooks/control.py"
        code.chmod(0o644)
        code.write_text("EXAMPLE = 'replaced'\n")
        code.chmod(0o444)
        with self.assertRaises(NamespaceError):
            config._namespace.runtime.validate()

    def test_registered_release_still_cannot_overlap_a_writable_project(self) -> None:
        runtime = self.runtime(self.pin())
        (self.base / ".git").mkdir()
        with tempfile.TemporaryDirectory(prefix="example-separate-home-") as directory:
            home = Path(directory)
            private = home / "authority"
            private.mkdir()
            config = ProcessNamespaceConfig(
                runtime, self.base, home, (private,), project_access="read-write"
            )
            with patch("hermes_codex_router.process_namespace._require_fd_bind_support"):
                with self.assertRaisesRegex(NamespaceError, "writable and other mounts overlap"):
                    config.wrap([str(runtime.executable)], {}, self.base)

    def test_real_namespace_keeps_user_owned_runtime_and_hooks_readonly(self) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        libraries = tuple(
            Path(value)
            for value in ("/usr/lib/python3.12", "/usr/lib/x86_64-linux-gnu", "/usr/lib64")
        )
        if not python.exists() or not all(path.exists() for path in libraries):
            namespace_unavailable(self, "system Python fixture unavailable")
        (self.release / "bin").chmod(0o755)
        shutil.copyfile(python, self.release / "bin/python")
        (self.release / "bin/python").chmod(0o555)
        (self.release / "bin").chmod(0o555)
        project, home, authority = (
            self.base / name for name in ("project", "session", "authority")
        )
        for directory in (project, home, authority):
            directory.mkdir(mode=0o700)
        (project / ".git").mkdir()
        (authority / "key").write_text("fictional-private-key")
        runtime = NamespaceRuntime(
            bwrap,
            self.release / "bin/python",
            (self.release, *libraries),
            code_root=self.release / "hooks",
            user_releases=(self.pin(),),
        )
        config = ProcessNamespaceConfig(
            runtime, project, home, (authority,), project_access="read-write"
        )
        source = (
            "import errno, json, pathlib, sys\n"
            "denied = []\n"
            "for name in sys.argv[1:]:\n"
            "    p = pathlib.Path(name)\n"
            "    assert p.is_file() and p.open('rb').read(1), 'missing positive control'\n"
            "    try: p.chmod(0o777)\n"
            "    except OSError as e: assert e.errno == errno.EROFS; denied.append(e.errno)\n"
            "    else: raise AssertionError('release chmod permitted')\n"
            "    try: p.write_text('changed')\n"
            "    except OSError as e: assert e.errno == errno.EROFS; denied.append(e.errno)\n"
            "    else: raise AssertionError('release write permitted')\n"
            "pathlib.Path('result').write_text('project-write')\n"
            "print(json.dumps(denied))\n"
        )
        with config.wrap(
            [
                str(runtime.executable),
                "-I",
                "-B",
                "-c",
                source,
                str(self.release / "hooks/control.py"),
                str(runtime.executable),
            ],
            {},
            project,
        ) as launch:
            completed = subprocess.run(
                launch.argv,
                env=launch.environment,
                pass_fds=launch.pass_fds,
                close_fds=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        if completed.returncode and namespace_permission_refused(completed.stderr):
            namespace_unavailable(self, "kernel disallows user namespaces")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)), 4)
        self.assertEqual((project / "result").read_text(), "project-write")
        self.assertEqual((self.release / "hooks/control.py").read_text(), "EXAMPLE = 'original'\n")
        self.pin().verify()
