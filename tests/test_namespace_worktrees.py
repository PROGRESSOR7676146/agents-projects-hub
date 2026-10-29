"""Real linked Git metadata, including a provider-free namespace commit."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.process_namespace as namespace
from hermes_codex_router.process_namespace import NamespaceError, NamespaceRuntime
from tests.fd_fixture import assert_descriptor_cleanup
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_mapped_host_root,
    require_namespace_runtime,
)


def setUpModule() -> None:
    require_mapped_host_root()


class NamespaceWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        from hermes_codex_router.process_namespace import ProcessNamespaceConfig

        temporary = tempfile.TemporaryDirectory(prefix="example-linked-namespace-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project, self.home, self.private = (
            self.base / name for name in ("project", "session", "authority")
        )
        for path in (self.project, self.home, self.private):
            path.mkdir(mode=0o700)
        self.runtime = NamespaceRuntime(
            Path(shutil.which("bwrap") or "/usr/bin/true"),
            Path("/usr/bin/true"),
            (Path("/usr/bin/true"),),
        )
        self.config = ProcessNamespaceConfig(self.runtime, self.project, self.home, (self.private,))
        self.main = self.base / "main"
        self.main.mkdir()
        self.git("init", "-b", "main", str(self.main))
        self.git("-C", str(self.main), "config", "user.name", "Example")
        self.git("-C", str(self.main), "config", "user.email", "author@example.com")
        self.git("-C", str(self.main), "commit", "--allow-empty", "-m", "Initial")
        self.git("-C", str(self.main), "worktree", "add", "-b", "lane", str(self.project))
        self.common = self.main / ".git"
        self.gitdir = self.common / "worktrees" / self.project.name
        self.developer = dataclasses.replace(
            self.config, project_access="read-write", git_access="read-write"
        )

    @staticmethod
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args],
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            stderr=subprocess.PIPE,
            text=True,
        ).strip()

    def test_linked_metadata_is_selected_without_main_or_sibling_worktree_mount(self) -> None:
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            self.developer.wrap(["/usr/bin/true"], {}, self.project) as launch,
        ):
            for path, flag in (
                (self.project / ".git", "--ro-bind-fd"),
                (self.gitdir, "--bind-fd"),
                (self.gitdir / "commondir", "--ro-bind-fd"),
                (self.gitdir / "gitdir", "--ro-bind-fd"),
                (self.common / "objects", "--bind-fd"),
                (self.common / "refs", "--bind-fd"),
                (self.common / "config", "--ro-bind-fd"),
            ):
                index = launch.argv.index(str(path))
                self.assertEqual(launch.argv[index - 2], flag)
                self.assertEqual(os.fstat(int(launch.argv[index - 1])).st_ino, path.stat().st_ino)
            self.assertNotIn(str(self.main), launch.argv)
            self.assertNotIn(str(self.common), launch.argv)
            self.assertNotIn(str(self.common / "worktrees"), launch.argv)

    def test_changed_or_symlinked_linked_metadata_refuses_and_closes_pins(self) -> None:
        for relative, replacement in (("gitdir", "/wrong/.git\n"), ("commondir", "/wrong\n")):
            target = self.gitdir / relative
            original = target.read_bytes()
            try:
                target.write_text(replacement)
                with assert_descriptor_cleanup(self), self.assertRaises(NamespaceError):
                    self.developer.wrap(["/usr/bin/true"], {}, self.project)
            finally:
                target.write_bytes(original)
        objects = self.common / "objects"
        objects.rename(self.common / "objects-original")
        objects.symlink_to(self.common / "objects-original", target_is_directory=True)
        with assert_descriptor_cleanup(self), self.assertRaises(NamespaceError):
            self.developer.wrap(["/usr/bin/true"], {}, self.project)

    def test_external_metadata_still_undergoes_private_authority_checks(self) -> None:
        configured = dataclasses.replace(self.developer, private_paths=(self.common / "objects",))
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            assert_descriptor_cleanup(self),
            self.assertRaisesRegex(NamespaceError, "private authority"),
        ):
            configured.wrap(["/usr/bin/true"], {}, self.project)

    def test_editor_keeps_all_linked_git_sources_readonly(self) -> None:
        editor = dataclasses.replace(self.config, project_access="read-write")
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            editor.wrap(["/usr/bin/true"], {}, self.project) as launch,
        ):
            for path in (self.gitdir, self.common / "objects", self.common / "refs"):
                index = launch.argv.index(str(path))
                self.assertEqual(launch.argv[index - 2], "--ro-bind-fd")

    def test_real_worktree_read_edit_commit_and_hidden_sources(self) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        roots = (
            python,
            Path("/usr/bin/git"),
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not all(p.exists() for p in roots):
            namespace_unavailable(self, "system Python/Git runtime fixture unavailable")
        try:
            runtime = NamespaceRuntime(bwrap, python, roots)
        except NamespaceError:
            namespace_unavailable(self, "system runtime is not immutable to worker")
        sibling = self.base / "sibling"
        self.git("-C", str(self.main), "worktree", "add", "-b", "sibling", str(sibling))
        (sibling / "private").write_text("fictional sibling material")
        hidden = self.project / ".hub" / "work"
        hidden.mkdir(parents=True)
        (hidden / "private").write_text("fictional operational material")
        (self.project / ".hub" / "staging").mkdir()
        source = r"""
import json, os, pathlib, subprocess, sys
common, sibling = map(pathlib.Path, sys.argv[1:3])
denied = {tuple(pair) for pair in json.loads(sys.argv[3])}
for process in pathlib.Path('/proc').iterdir():
    if not process.name.isdigit(): continue
    try:
        descriptors = list((process / 'fd').iterdir())
    except (PermissionError, FileNotFoundError):
        continue
    for descriptor in descriptors:
        try:
            info = descriptor.stat()
        except (PermissionError, FileNotFoundError):
            continue
        assert (info.st_dev, info.st_ino) not in denied, str(descriptor)
for path in (common / 'HEAD', common / 'index',
             common / 'worktrees/sibling', sibling / 'private',
             pathlib.Path('.hub/work/private')):
    assert not path.exists(), str(path)
for path in (pathlib.Path('.git'), common / 'config'):
    try:
        path.open('ab').close()
    except OSError:
        pass
    else:
        raise AssertionError('control file writable')
pathlib.Path('work.txt').write_text('edited\n')
def git(*args):
    return subprocess.check_output(['/usr/bin/git', *args], stderr=subprocess.STDOUT, text=True)
assert git('rev-parse', '--show-toplevel').strip() == str(pathlib.Path.cwd())
git('add', 'work.txt')
git('commit', '-m', 'Namespace worktree edit')
pathlib.Path('.hub/staging/result.txt').write_text('deliverable')
print(json.dumps({'head': git('rev-parse', 'HEAD').strip(), 'status': git('status', '--porcelain')}))
"""
        configured = dataclasses.replace(self.developer, runtime=runtime)
        main_head = self.git("-C", str(self.main), "rev-parse", "HEAD")
        sibling_head = self.git("-C", str(sibling), "rev-parse", "HEAD")
        with configured.wrap(
            [str(python), "-I", "-c", source, str(self.common), str(sibling), "[]"],
            {},
            self.project,
        ) as launch:
            argv = list(launch.argv)
            argv[-1] = json.dumps(
                [[info.st_dev, info.st_ino] for info in (os.fstat(fd) for fd in launch.pass_fds)]
            )
            run = subprocess.run(
                argv,
                env=launch.environment,
                cwd="/",
                pass_fds=launch.pass_fds,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if run.returncode and namespace_permission_refused(run.stderr):
            namespace_unavailable(self, "kernel namespace permission refused")
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        result = json.loads(run.stdout)
        self.assertNotEqual(result["head"], main_head)
        self.assertEqual(self.git("-C", str(self.main), "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git("-C", str(sibling), "rev-parse", "HEAD"), sibling_head)
        self.assertEqual((self.project / "work.txt").read_text(), "edited\n")
