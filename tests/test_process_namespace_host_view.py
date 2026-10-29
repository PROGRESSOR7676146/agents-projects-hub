"""Real OS witness for the whole-host readonly view of the workstation profile."""

from __future__ import annotations

import errno
import json
import shutil
import socket
import subprocess
import tempfile
import threading
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

_NESTED = r"""
import ctypes, json, os
def nested():
    # A child enters a nested user and mount namespace, proves mount works
    # there, then tries to clear only the readonly flag of the inherited root.
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        report = {}
        uid, gid = os.getuid(), os.getgid()
        try:
            os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
            report['unshare'] = 0
        except OSError as error:
            report['unshare'] = error.errno
        if report['unshare'] == 0:
            # Nested bwrap and unshare -U write these maps through /proc.
            try:
                for name, line in (('uid_map', f'0 {uid} 1'), ('setgroups', 'deny'),
                                   ('gid_map', f'0 {gid} 1')):
                    with open(f'/proc/self/{name}', 'w') as stream:
                        stream.write(line)
                report['map'] = 0
            except OSError as error:
                report['map'] = error.errno
            libc = ctypes.CDLL(None, use_errno=True)
            def mount(*args):
                return 0 if libc.mount(*args) == 0 else ctypes.get_errno()
            report['control'] = mount(b'none', b'/tmp', b'tmpfs', 0, None)
            flags = os.statvfs('/').f_flag
            kept = 0
            for st, ms in ((os.ST_NOSUID, 2), (os.ST_NODEV, 4), (os.ST_NOEXEC, 8),
                           (os.ST_NOATIME, 1024), (os.ST_NODIRATIME, 2048),
                           (os.ST_RELATIME, 1 << 21)):
                if flags & st:
                    kept |= ms
            report['root_readonly'] = bool(flags & os.ST_RDONLY)
            # MS_REMOUNT | MS_BIND with every other flag kept, MS_RDONLY cleared.
            report['unlock'] = mount(None, b'/', None, 32 | 4096 | kept, None)
        os.write(writer, json.dumps(report).encode())
        os._exit(0)
    os.close(writer)
    with os.fdopen(reader) as stream:
        report = json.loads(stream.read())
    os.waitpid(pid, 0)
    return report
"""

_WITNESS = (
    _NESTED
    + r"""
import json, os, pathlib, socket, sys
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
result['proc_sys_write'] = write('/proc/sys/vm/swappiness')
result['nested'] = nested()
result['run'] = pathlib.Path('/run').is_dir()
result['run_write'] = write('/run/hub-host-view-probe')
with socket.socket(socket.AF_UNIX) as client:
    client.connect(inputs['socket'])
    client.sendall(b'witness')
print(json.dumps(result))
"""
)


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
        # Outside the profile first: a host that refuses nested namespaces to
        # this test process cannot witness the profile's nested sandboxes.
        python, roots = _python_runtime()
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        probe = subprocess.run(
            (str(python), "-I", "-c", _NESTED + "print(json.dumps(nested()))"),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        if json.loads(probe.stdout)["unshare"] != 0:
            namespace_unavailable(self, "host refuses nested user namespaces")
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
            # A short socket parent: a deep HOME can exceed the AF_UNIX path limit.
            control = Path(tempfile.mkdtemp(prefix="hub-socket-"))
            self.addCleanup(shutil.rmtree, control, True)
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(control / "permission.sock"))
            (control / "permission.sock").chmod(0o600)
            listener.listen(1)
            listener.settimeout(30)
            received: list[bytes] = []

            def serve() -> None:
                connection, _ = listener.accept()
                with connection:
                    received.append(connection.recv(64))

            server = threading.Thread(target=serve, daemon=True)
            server.start()
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
                network="shared",
                permission_socket=control / "permission.sock",
                host_view=True,
            )
            inputs = {"neighbor": str(neighbor), "socket": config.socket_destination}
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
            server.join(5)
            self.assertEqual(received, [b"witness"])
            self.assertEqual(config.socket_destination, "/tmp/hub-permission.sock")
            # Nested namespaces work where the host admits them, and the
            # inherited readonly root stays locked inside them.
            self.assertEqual(
                result["nested"],
                {
                    "unshare": 0,
                    "map": 0,
                    "control": 0,
                    "root_readonly": True,
                    "unlock": errno.EPERM,
                },
            )
            # Host /run stays visible, readonly, instead of a fresh tmpfs.
            self.assertTrue(result["run"])
            # /proc stays writable for nested maps; host sysctls stay denied.
            for key in (
                "work_write",
                "neighbor_write",
                "system_write",
                "run_write",
                "proc_sys_write",
            ):
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
