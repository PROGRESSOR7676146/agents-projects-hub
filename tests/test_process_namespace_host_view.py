"""Real OS witness for the whole-host readonly view of the workstation profile."""

from __future__ import annotations

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
)
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)

_WITNESS = r"""
import json, os, pathlib, sys
inputs = json.loads(sys.argv[1])
result = {}
def read(path):
    try:
        return pathlib.Path(path).read_text()
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
result['work'] = read('.hub/work/receipt')
result['project_write'] = write('new-file')
result['work_write'] = write('.hub/work/new-file')
result['neighbor_write'] = write(inputs['neighbor'] + '/new-file')
result['system_write'] = write('/usr/hub-host-view-probe')
result['home'] = os.environ['HOME']
result['home_write'] = write(os.environ['HOME'] + '/new-file')
result['tmp_write'] = write('/tmp/new-file')
result['system'] = pathlib.Path('/usr/share').is_dir()
result['proc_self'] = pathlib.Path('/proc/self/status').exists()
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


class HostViewWitnessTests(unittest.TestCase):
    def test_host_is_readonly_except_project_home_and_tmp(self) -> None:
        for existing_work in (True, False):
            with self.subTest(existing_work=existing_work):
                self._witness(existing_work)

    def _witness(self, existing_work: bool) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python, roots = _python_runtime()
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        with tempfile.TemporaryDirectory(prefix="example-host-", dir=Path.home()) as raw:
            base = Path(raw)
            project, neighbor, home = (base / name for name in ("project", "neighbor", "home"))
            for path in (project, neighbor, home):
                path.mkdir(mode=0o700)
            (project / ".git").mkdir()
            (project / ".git" / "HEAD").write_text("fictional-head", encoding="utf-8")
            if existing_work:
                (project / ".hub" / "work").mkdir(parents=True)
                (project / ".hub" / "work" / "receipt").write_text("fictional", encoding="utf-8")
            (neighbor / "README").write_text("neighbor-source", encoding="utf-8")
            (neighbor / ".env").write_text("FICTIONAL_TOKEN=neighbor", encoding="utf-8")
            try:
                runtime = NamespaceRuntime(bwrap, python, roots)
            except NamespaceError:
                namespace_unavailable(self, "system runtime is not immutable to worker")
            config = ProcessNamespaceConfig(
                runtime,
                project,
                home,
                (base / "authority",),
                project_access="read-write",
                host_view=True,
            )
            inputs = {"neighbor": str(neighbor)}
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
            self.assertEqual(result["neighbor_env"], "FICTIONAL_TOKEN=neighbor")
            if existing_work:
                self.assertEqual(result["work"], "fictional")
            self.assertEqual(result["home"], str(home))
            for key in ("project_write", "home_write", "tmp_write"):
                self.assertEqual(result[key], "written", key)
            for key in ("work_write", "neighbor_write", "system_write"):
                self.assertIsInstance(result[key], int, key)
            self.assertTrue(result["system"])
            self.assertTrue(result["proc_self"])
            self.assertTrue((project / "new-file").exists())
            self.assertTrue((home / "new-file").exists())
            self.assertFalse((neighbor / "new-file").exists())
            # A missing operational directory is created first, then guarded.
            self.assertTrue((project / ".hub" / "work").is_dir())
            self.assertFalse((project / ".hub" / "work" / "new-file").exists())

    def test_host_view_requires_a_writable_project(self) -> None:
        with self.assertRaisesRegex(NamespaceError, "writable project"):
            ProcessNamespaceConfig(
                object(),  # type: ignore[arg-type]
                Path("/home/example/project"),
                Path("/home/example/session"),
                (Path("/home/example/private"),),
                host_view=True,
            )


if __name__ == "__main__":
    unittest.main()
