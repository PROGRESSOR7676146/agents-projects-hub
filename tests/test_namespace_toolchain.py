"""Explicit developer tools remain readonly and usable inside the namespace."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_codex_router.process_namespace as namespace
from hermes_codex_router.namespace_toolchain import NamespaceToolchain, parse_toolchain
from tests import test_namespace_worktrees as fixtures
from tests.fd_fixture import assert_descriptor_cleanup
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)


class NamespaceToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.NamespaceWorktreeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.tools = fixture.base / "tools"
        self.bin = self.tools / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "python3").symlink_to("/usr/bin/python3.12")
        self.toolchain = NamespaceToolchain(
            roots=(self.tools,),
            path=(self.bin,),
            git_hooks_path=fixture.project / ".githooks",
            git_python=Path("/usr/bin/true"),
            git_identity=("Example", "author@example.com"),
        )
        (fixture.project / ".githooks").mkdir()
        self.config = dataclasses.replace(fixture.developer, toolchain=self.toolchain)

    def test_readonly_tools_path_and_git_settings_are_launch_local(self) -> None:
        with (
            patch.object(namespace, "_require_fd_bind_support"),
            self.config.wrap(["/usr/bin/true"], {}, self.fixture.project) as launch,
        ):
            index = launch.argv.index(str(self.tools))
            self.assertEqual(launch.argv[index - 2], "--ro-bind-fd")
            self.assertEqual(launch.environment["PATH"], f"{self.bin}:/usr/bin:/bin")
            self.assertEqual(launch.environment["HUB_PUBLISH_PYTHON"], "/usr/bin/true")
            count = int(launch.environment["GIT_CONFIG_COUNT"])
            config = {
                launch.environment[f"GIT_CONFIG_KEY_{i}"]: launch.environment[
                    f"GIT_CONFIG_VALUE_{i}"
                ]
                for i in range(count)
            }
            self.assertEqual(config["core.hooksPath"], str(self.fixture.project / ".githooks"))
            self.assertEqual(config["gc.auto"], "0")
            self.assertEqual(config["maintenance.auto"], "false")
            self.assertEqual(config["branch.autoSetupMerge"], "false")
            self.assertEqual(config["user.email"], "author@example.com")
        self.assertEqual(
            self.fixture.git("-C", str(self.fixture.main), "config", "user.name"), "Example"
        )

    def test_private_project_home_overlap_and_hardlinks_refuse_without_fd_leaks(self) -> None:
        secret = self.tools / "secret"
        secret.write_text("fictional")
        for configured in (
            dataclasses.replace(self.config, private_paths=(secret,)),
            dataclasses.replace(
                self.config,
                toolchain=dataclasses.replace(self.toolchain, roots=(self.fixture.project,)),
            ),
            dataclasses.replace(
                self.config,
                toolchain=dataclasses.replace(self.toolchain, roots=(self.fixture.home,)),
            ),
        ):
            with (
                self.subTest(configured=configured),
                assert_descriptor_cleanup(self),
                self.assertRaises(namespace.NamespaceError),
            ):
                configured.wrap(["/usr/bin/true"], {}, self.fixture.project)
        os.link(secret, self.tools / "linked")
        with (
            assert_descriptor_cleanup(self),
            self.assertRaisesRegex(namespace.NamespaceError, "hardlink"),
        ):
            self.config.wrap(["/usr/bin/true"], {}, self.fixture.project)

    def test_path_and_hook_must_be_visible_and_sources_canonical(self) -> None:
        for replacement in (
            {"path": (self.fixture.private,)},
            {"git_hooks_path": self.fixture.private},
            {"git_python": self.fixture.private / "python"},
        ):
            configured = dataclasses.replace(
                self.config, toolchain=dataclasses.replace(self.toolchain, **replacement)
            )
            with (
                self.subTest(replacement=replacement),
                assert_descriptor_cleanup(self),
                self.assertRaises(namespace.NamespaceError),
            ):
                configured.wrap(["/usr/bin/true"], {}, self.fixture.project)
        linked = self.fixture.base / "linked-tools"
        linked.symlink_to(self.tools)
        configured = dataclasses.replace(
            self.config, toolchain=dataclasses.replace(self.toolchain, roots=(linked,))
        )
        with self.assertRaises(namespace.NamespaceError):
            configured.wrap(["/usr/bin/true"], {}, self.fixture.project)

    def test_config_is_explicit_bounded_and_rejects_unknown_or_unsafe_values(self) -> None:
        raw = {"roots": [str(self.tools)], "path": [str(self.bin)]}
        self.assertEqual(parse_toolchain(raw).roots, (self.tools,))
        for change in (
            {"roots": []},
            {"roots": [str(self.tools)] * 2},
            {"path": ["relative"]},
            {"path": ["/tmp/a:b"]},
            {"path": ["/tmp/a\n"]},
            {"roots": [f"/opt/tool-{i}" for i in range(33)]},
            {"automatic_approval": True},
            {"git_identity": ["Example", "bad\nidentity"]},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_toolchain(raw | change)

    def test_real_branch_python_node_build_and_failing_then_passing_hook(self) -> None:
        f = self.fixture
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        node = Path(shutil.which("node") or "/usr/bin/node").resolve()
        roots = (
            python,
            Path("/usr/bin/git"),
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not all(p.exists() for p in (*roots, node)):
            namespace_unavailable(self, "Python/Node/Git fixture unavailable")
        # Node's own prefix includes npm and its modules. System Node uses its
        # exact immutable binary; both layouts exercise Python aliases via PATH.
        if str(node).startswith("/usr/"):
            runtime = namespace.NamespaceRuntime(bwrap, python, (*roots, node))
            toolchain = self.toolchain
        else:
            runtime = namespace.NamespaceRuntime(bwrap, python, roots)
            copied_node = self.bin / "node"
            shutil.copyfile(node, copied_node)
            copied_node.chmod(0o755)
            node = copied_node
            toolchain = self.toolchain
        toolchain = dataclasses.replace(toolchain, git_python=python)
        hook = f.project / ".githooks/pre-commit"
        hook.write_text(
            f"#!{python}\nfrom pathlib import Path\nraise SystemExit(1 if Path('reject').exists() else 0)\n"
        )
        hook.chmod(0o755)
        f.git("-C", str(f.main), "update-ref", "refs/remotes/origin/main", "HEAD")
        sibling = f.base / "sibling"
        f.git("-C", str(f.main), "worktree", "add", "-b", "sibling", str(sibling))
        before = f.git("-C", str(f.main), "rev-parse", "HEAD")
        config_before = (f.common / "config").read_bytes()
        source = r"""
import json, pathlib, shutil, subprocess, sys
def run(*args):
    return subprocess.run(args, capture_output=True, text=True)
def git(*args):
    result = run('/usr/bin/git', *args)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout.strip()
assert shutil.which('python3')
assert run('python3', '-c', 'assert 2+2 == 4').returncode == 0
assert run(sys.argv[1], '-e', "require('node:assert').equal(2+2,4);require('node:fs').writeFileSync('built.js','// built')").returncode == 0
git('switch', '-c', 'namespace-feature', 'origin/main')
assert git('branch', '--show-current') == 'namespace-feature'
pathlib.Path('work.txt').write_text('tested')
pathlib.Path('reject').touch()
git('add', 'work.txt', 'built.js', '.githooks/pre-commit')
assert run('/usr/bin/git', 'commit', '-m', 'Rejected').returncode != 0
assert git('rev-parse', 'HEAD') == sys.argv[2]
pathlib.Path('reject').unlink()
git('commit', '-m', 'Development cycle')
assert not git('status', '--porcelain')
try:
    pathlib.Path(sys.argv[3]).write_text('overwrite')
except OSError:
    pass
else:
    raise AssertionError('toolchain writable')
print(json.dumps({'branch': git('branch', '--show-current'), 'head': git('rev-parse', 'HEAD')}))
"""
        configured = dataclasses.replace(self.config, runtime=runtime, toolchain=toolchain)
        with configured.wrap(
            [str(python), "-I", "-c", source, str(node), before, str(self.bin / "python3")],
            {},
            f.project,
        ) as launch:
            run = subprocess.run(
                launch.argv,
                env=launch.environment,
                pass_fds=launch.pass_fds,
                capture_output=True,
                text=True,
                cwd="/",
                timeout=30,
            )
        if run.returncode and namespace_permission_refused(run.stderr):
            namespace_unavailable(self, "kernel namespace permission refused")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(run.stdout)
        self.assertNotEqual(result["head"], before)
        self.assertEqual(f.git("-C", str(f.main), "rev-parse", "HEAD"), before)
        self.assertEqual(f.git("-C", str(sibling), "rev-parse", "HEAD"), before)
        self.assertEqual((f.common / "config").read_bytes(), config_before)
