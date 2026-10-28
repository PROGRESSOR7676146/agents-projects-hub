"""Real OS witness for the wide readonly view used by the workstation profile."""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.process_namespace import (
    NamespaceError,
    NamespaceRuntime,
    ProcessNamespaceConfig,
    _masked_entries,
)
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)

_WITNESS = r"""
import errno, json, os, pathlib, sys
inputs = json.loads(sys.argv[1])
result = {}
def read(path):
    try:
        return pathlib.Path(path).read_text()
    except OSError as error:
        return error.errno
def listing(path):
    try:
        return sorted(os.listdir(path))
    except OSError as error:
        return error.errno
def write(path):
    try:
        pathlib.Path(path).write_text('x')
    except OSError as error:
        return error.errno
    return 'written'
result['neighbor'] = read(inputs['neighbor'] + '/README')
result['neighbor_env'] = read(inputs['neighbor'] + '/.env')
result['neighbor_env_dir'] = listing(inputs['neighbor'] + '/.env.d')
result['neighbor_work'] = listing(inputs['neighbor'] + '/.hub/work')
result['root_env'] = read(inputs['sources'] + '/.env.local')
result['root_env_dir'] = listing(inputs['sources'] + '/.env.d')
result['upper_env'] = read(inputs['neighbor'] + '/.ENV')
result['alias_env'] = read(inputs['sources'] + '/current/.env')
result['alias_work'] = listing(inputs['sources'] + '/current/.hub/work')
result['alias_readme'] = read(inputs['sources'] + '/current/README')
result['intermediate_work'] = listing(inputs['sources'] + '/linked/.hub/work')
result['intermediate_alias'] = listing(inputs['sources'] + '/current-work')
result['project_env'] = read('.env')
result['project_work'] = listing('.hub/work')
result['project_write'] = write('new-file')
result['neighbor_write'] = write(inputs['neighbor'] + '/new-file')
result['system_write'] = write('/usr/hub-visible-probe')
result['private'] = read(inputs['private'])
result['system'] = pathlib.Path(inputs['system']).exists()
print(json.dumps(result))
"""


def _python_runtime() -> tuple[Path, tuple[Path, ...]]:
    python = Path("/usr/bin/python3.12")
    roots = (
        python,
        Path("/usr/lib/python3.12"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib64"),
    )
    return python, roots


class VisibleRootWitnessTests(unittest.TestCase):
    def test_visible_trees_are_readonly_with_operational_and_env_material_masked(self) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python, roots = _python_runtime()
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        # Visible trees cannot live under /tmp, so the fixture uses the worker home.
        with tempfile.TemporaryDirectory(prefix="example-visible-", dir=Path.home()) as raw:
            base = Path(raw)
            sources, home, authority = (base / name for name in ("sources", "session", "authority"))
            project, neighbor = sources / "project", sources / "neighbor"
            for path in (sources, home, authority, project, neighbor):
                path.mkdir(mode=0o700)
            (project / ".git").mkdir()
            (project / ".git" / "HEAD").write_text("fictional-head", encoding="utf-8")
            (project / ".env").write_text("FICTIONAL_TOKEN=project", encoding="utf-8")
            (project / ".hub" / "work").mkdir(parents=True)
            (project / ".hub" / "work" / "receipt").write_text("fictional", encoding="utf-8")
            (neighbor / "README").write_text("neighbor-source", encoding="utf-8")
            (neighbor / ".env").write_text("FICTIONAL_TOKEN=neighbor", encoding="utf-8")
            (neighbor / ".env.d").mkdir()
            (neighbor / ".env.d" / "key").write_text("fictional", encoding="utf-8")
            (neighbor / ".hub" / "work").mkdir(parents=True)
            (neighbor / ".hub" / "work" / "state.db").write_text("fictional", encoding="utf-8")
            (sources / ".env.local").write_text("FICTIONAL_TOKEN=root", encoding="utf-8")
            # A masked directory hides its own .env entries without a nested mask.
            (sources / ".env.d").mkdir()
            (sources / ".env.d" / ".env.local").write_text("fictional", encoding="utf-8")
            (neighbor / ".ENV").write_text("FICTIONAL_TOKEN=upper", encoding="utf-8")
            # A symlinked child checkout deeper in the tree is masked at its target.
            deep = sources / "archive" / "team" / "repo"
            (deep / ".hub" / "work").mkdir(parents=True)
            (deep / ".hub" / "work" / "receipt").write_text("fictional", encoding="utf-8")
            (deep / ".env").write_text("FICTIONAL_TOKEN=deep", encoding="utf-8")
            (deep / "README").write_text("deep-source", encoding="utf-8")
            (sources / "current").symlink_to(Path("archive") / "team" / "repo")
            # Symlinked intermediate components canonicalize before nesting checks.
            (sources / "linked").mkdir()
            (sources / "linked" / ".hub").symlink_to(Path("..") / "archive" / "team")
            (sources / "archive" / "team" / "work").mkdir()
            (sources / "archive" / "team" / "work" / ".env").write_text("x", encoding="utf-8")
            (sources / "current-work").symlink_to(Path("archive") / "team" / "work")
            secret = authority / "key"
            secret.write_text("fictional-private-key", encoding="utf-8")
            try:
                runtime = NamespaceRuntime(bwrap, python, roots)
            except NamespaceError:
                namespace_unavailable(self, "system runtime is not immutable to worker")
            config = ProcessNamespaceConfig(
                runtime,
                project,
                home,
                (authority,),
                project_access="read-write",
                visible_roots=(Path("/usr"), sources),
            )
            inputs = {
                "neighbor": str(neighbor),
                "sources": str(sources),
                "private": str(secret),
                "system": "/usr/share",
            }
            argv = (str(python), "-I", "-c", _WITNESS, json.dumps(inputs))
            with config.wrap(argv, {}, project) as launch:
                completed = subprocess.run(
                    launch.argv,
                    env=launch.environment,
                    pass_fds=launch.pass_fds,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    close_fds=True,
                )
            if completed.returncode and namespace_permission_refused(completed.stderr):
                namespace_unavailable(self, "kernel refused namespace creation")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["neighbor"], "neighbor-source")
            self.assertEqual(result["alias_readme"], "deep-source")
            for key in ("neighbor_env", "root_env", "project_env", "upper_env", "alias_env"):
                # A device-node mask under nodev denies the open: no content either way.
                self.assertIn(result[key], ("", errno.EACCES), key)
            for key in (
                "neighbor_env_dir",
                "neighbor_work",
                "project_work",
                "root_env_dir",
                "alias_work",
                "intermediate_work",
                "intermediate_alias",
            ):
                self.assertEqual(result[key], [], key)
            self.assertEqual(result["project_write"], "written")
            self.assertIsInstance(result["neighbor_write"], int)
            self.assertIsInstance(result["system_write"], int)
            self.assertIsInstance(result["private"], int)
            self.assertTrue(result["system"])
            self.assertTrue((project / "new-file").exists())
            self.assertFalse((neighbor / "new-file").exists())
            self.assertEqual((neighbor / ".env").read_text(), "FICTIONAL_TOKEN=neighbor")


class MaskEnumerationTests(unittest.TestCase):
    def test_aliases_into_any_identity_mount_are_masked_at_their_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-visible-") as raw:
            base = Path(raw).resolve()
            sources, tools = base / "sources", base / "tools"
            checkout = tools / "checkout"
            (checkout / ".hub" / "work").mkdir(parents=True)
            (checkout / ".env").write_text("x", encoding="utf-8")
            sources.mkdir()
            (sources / "current").symlink_to(checkout)
            (sources / "outside").symlink_to(base / "unmounted")
            masks = _masked_entries(sources, (sources, tools))
            self.assertEqual(
                sorted(masks),
                [(checkout / ".env", False), (checkout / ".hub" / "work", True)],
            )
            self.assertEqual(_masked_entries(sources, (sources,)), [])


class VisibleRootValidationTests(unittest.TestCase):
    def _config(self, base: Path, *visible: Path, private: Path | None = None) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python, roots = _python_runtime()
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        try:
            runtime = NamespaceRuntime(bwrap, python, roots)
        except NamespaceError:
            namespace_unavailable(self, "system runtime is not immutable to worker")
        project, home = base / "sources" / "project", base / "session"
        config = ProcessNamespaceConfig(
            runtime,
            project,
            home,
            (private or base / "authority",),
            project_access="read-write",
            visible_roots=visible,
        )
        with config.wrap((str(python), "-c", "pass"), {}, project):
            pass

    def _fixture(self, raw: str) -> Path:
        base = Path(raw)
        for path in (base / "sources" / "project" / ".git", base / "session", base / "authority"):
            path.mkdir(parents=True, mode=0o700)
        (base / "session").chmod(0o700)
        return base

    def test_host_state_trees_and_broad_roots_refuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-visible-", dir=Path.home()) as raw:
            base = self._fixture(raw)
            for visible in (Path("/var"), Path("/tmp"), Path("/home"), Path.home()):
                with self.subTest(visible=visible), self.assertRaises(NamespaceError):
                    self._config(base, visible)

    def test_visible_tree_cannot_contain_session_home_private_path_or_sit_in_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="example-visible-", dir=Path.home()) as raw:
            base = self._fixture(raw)
            (base / "sources" / "project" / "inner").mkdir()
            with self.assertRaisesRegex(NamespaceError, "visible and writable"):
                self._config(base, base)
            with self.assertRaisesRegex(NamespaceError, "visible and writable"):
                self._config(base, base / "sources" / "project" / "inner")
            private = base / "sources" / "secrets"
            private.mkdir()
            with self.assertRaisesRegex(NamespaceError, "private authority"):
                self._config(base, base / "sources", private=private)


if __name__ == "__main__":
    unittest.main()
