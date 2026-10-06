"""Real OS witness for a provider-free read-only/private-network consumer."""

from __future__ import annotations

import array
import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

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
import json, os, pathlib, socket, subprocess, sys
inputs = json.loads(sys.argv[1])
role = sys.argv[2]
result = {'reads': {}, 'writes': {}, 'network': {}, 'fd_tables': {}, 'leaked_fds': []}
for name, path in {
    'private': inputs['private'],
    'private_link': 'private-link',
    'proc_root': '/proc/self/root' + inputs['private'],
    'permission_socket': '/run/hub-permission.sock',
}.items():
    try:
        pathlib.Path(path).read_bytes()
    except OSError as error:
        result['reads'][name] = error.errno
    else:
        raise AssertionError('hidden path readable: ' + name)
for name, path in {
    'project': 'visible', 'new_file': 'new-file', 'git': '.git/HEAD',
    'runtime': sys.executable,
}.items():
    flags = os.O_WRONLY | (os.O_CREAT if name == 'new_file' else 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        result['writes'][name] = error.errno
    else:
        os.close(descriptor)
        raise AssertionError('readonly source writable: ' + name)
denied_inodes = {tuple(identity) for identity in inputs['denied_inodes']}
for process in pathlib.Path('/proc').iterdir():
    if not process.name.isdigit(): continue
    try:
        descriptors = list(process.joinpath('fd').iterdir())
    except PermissionError:
        result['fd_tables'][process.name] = 'inaccessible'
        continue
    except FileNotFoundError:
        continue
    result['fd_tables'][process.name] = 'readable'
    for descriptor in descriptors:
        try:
            info = descriptor.stat()
        except (PermissionError, FileNotFoundError):
            continue
        if (info.st_dev, info.st_ino) in denied_inodes:
            result['leaked_fds'].append(str(descriptor))
result['self_pid'] = str(os.getpid())
for name, family, endpoint in (
    ('tcp', socket.AF_INET, ('127.0.0.1', inputs['port'])),
    ('abstract', socket.AF_UNIX, '\0' + inputs['abstract']),
    ('pathname', socket.AF_UNIX, inputs['pathname']),
):
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        try:
            connection.connect(endpoint)
        except OSError as error:
            result['network'][name] = error.errno
        else:
            raise AssertionError('host endpoint reachable: ' + name)
result['visible'] = pathlib.Path('visible').read_text()
result['netns'] = os.readlink('/proc/self/ns/net')
result['cap_eff'] = next(line.split()[1] for line in
    pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
pathlib.Path('/home/example/' + role).write_text('session-' + role)
if role == 'parent':
    child = subprocess.run(
        [sys.executable, '-I', '-c', inputs['source'], sys.argv[1], 'child'],
        capture_output=True, text=True, timeout=10, check=False, close_fds=True,
    )
    if child.returncode:
        raise AssertionError('exec child failed: ' + child.stderr)
    result['child'] = json.loads(child.stdout)
print(json.dumps(result))
"""


class ProcessNamespaceRehearsalTests(unittest.TestCase):
    def test_readonly_private_network_parent_and_exec_child(self) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        roots = (
            python,
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        with tempfile.TemporaryDirectory(prefix="example-readonly-witness-") as directory:
            base = Path(directory)
            project, home, authority = (base / name for name in ("project", "session", "authority"))
            for path in (project, home, authority):
                path.mkdir(mode=0o700)
            (project / ".git").mkdir()
            (project / ".git" / "HEAD").write_text("fictional-head", encoding="utf-8")
            (project / "visible").write_text("authorized-material", encoding="utf-8")
            secret = authority / "key"
            secret.write_text("fictional-private-key", encoding="utf-8")
            secret.chmod(0o600)
            (project / "private-link").symlink_to(secret)
            try:
                tcp_listener = socket.socket(socket.AF_INET)
            except PermissionError:
                namespace_unavailable(self, "local socket fixture unavailable")
            with (
                tcp_listener as tcp,
                socket.socket(socket.AF_UNIX) as pathname,
                socket.socket(socket.AF_UNIX) as abstract,
                secret.open("rb") as secret_handle,
            ):
                pathname_address = authority / "hidden.sock"
                abstract_name = "example-readonly-" + uuid4().hex
                try:
                    tcp.bind(("127.0.0.1", 0))
                    pathname.bind(str(pathname_address))
                    abstract.bind("\0" + abstract_name)
                except PermissionError:
                    namespace_unavailable(self, "local socket fixture unavailable")
                for listener in (tcp, pathname, abstract):
                    listener.listen(1)
                    listener.settimeout(3)
                # Every target exists and is reachable outside isolation. The
                # abstract service really transfers the hidden synthetic inode.
                for family, address, listener in (
                    (socket.AF_INET, tcp.getsockname(), tcp),
                    (socket.AF_UNIX, str(pathname_address), pathname),
                    (socket.AF_UNIX, "\0" + abstract_name, abstract),
                ):
                    with socket.socket(family) as control:
                        control.settimeout(3)
                        control.connect(address)
                        accepted, _ = listener.accept()
                        with accepted:
                            if listener is abstract:
                                accepted.sendmsg(
                                    [b"x"],
                                    [
                                        (
                                            socket.SOL_SOCKET,
                                            socket.SCM_RIGHTS,
                                            array.array("i", [secret_handle.fileno()]),
                                        )
                                    ],
                                )
                                data, ancillary, _, _ = control.recvmsg(
                                    1, socket.CMSG_SPACE(array.array("i").itemsize)
                                )
                                self.assertEqual(data, b"x")
                                received = array.array("i")
                                for level, kind, payload in ancillary:
                                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                                        received.frombytes(payload)
                                try:
                                    self.assertEqual(len(received), 1)
                                    self.assertEqual(
                                        os.pread(received[0], 128, 0), b"fictional-private-key"
                                    )
                                finally:
                                    for descriptor in received:
                                        os.close(descriptor)
                try:
                    runtime = NamespaceRuntime(bwrap, python, roots)
                except NamespaceError:
                    namespace_unavailable(self, "system runtime is not immutable to worker")
                config = ProcessNamespaceConfig(runtime, project, home, (authority,))
                with socket.create_connection(tcp.getsockname(), timeout=3) as inherited_socket:
                    accepted, _ = tcp.accept()
                    self.addCleanup(accepted.close)
                    accepted.settimeout(3)
                    control_payload = b"positive-channel-control"
                    accepted.sendall(control_payload)
                    received_control = bytearray()
                    while len(received_control) < len(control_payload):
                        part = inherited_socket.recv(len(control_payload) - len(received_control))
                        self.assertTrue(part, "positive channel closed before complete control")
                        received_control.extend(part)
                    self.assertEqual(received_control, control_payload)
                    inheritable = [secret_handle.fileno(), inherited_socket.fileno()]
                    for descriptor in inheritable:
                        os.set_inheritable(descriptor, True)
                    inputs = {
                        "private": str(secret),
                        "denied_inodes": [],
                        "port": tcp.getsockname()[1],
                        "abstract": abstract_name,
                        "pathname": str(pathname_address),
                        "source": _WITNESS,
                    }
                    before = len(list(Path("/proc/self/fd").iterdir()))
                    with config.wrap(
                        [str(python), "-I", "-c", _WITNESS, json.dumps(inputs), "parent"],
                        {},
                        project,
                    ) as launch:
                        self.assertTrue(set(inheritable).isdisjoint(launch.pass_fds))
                        inputs["denied_inodes"] = [
                            [info.st_dev, info.st_ino]
                            for info in (
                                os.fstat(fd)
                                for fd in (
                                    *inheritable,
                                    tcp.fileno(),
                                    pathname.fileno(),
                                    abstract.fileno(),
                                    accepted.fileno(),
                                    *launch.pass_fds,
                                )
                            )
                        ]
                        # Build the immutable launch first so the witness also
                        # checks every consumed mount pin by inode, not FD number.
                        argv = list(launch.argv)
                        argv[-2] = json.dumps(inputs)
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
                    self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)
                    accepted.close()
                if run.returncode and namespace_permission_refused(run.stderr):
                    namespace_unavailable(self, "kernel disallows user namespaces")
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(run.stdout)
                child = result.pop("child")
                for observed in (result, child):
                    self.assertEqual(observed["visible"], "authorized-material")
                    self.assertNotEqual(observed["netns"], os.readlink("/proc/self/ns/net"))
                    self.assertEqual(int(observed["cap_eff"], 16), 0)
                    self.assertEqual(
                        set(observed["reads"]),
                        {"private", "private_link", "proc_root", "permission_socket"},
                    )
                    self.assertTrue(
                        all(
                            value in (errno.ENOENT, errno.EACCES)
                            for value in observed["reads"].values()
                        )
                    )
                    self.assertEqual(
                        set(observed["writes"]), {"project", "new_file", "git", "runtime"}
                    )
                    self.assertEqual(
                        {
                            name: value
                            for name, value in observed["writes"].items()
                            if name != "runtime"
                        },
                        {"project": errno.EROFS, "new_file": errno.EROFS, "git": errno.EROFS},
                    )
                    self.assertIn(observed["writes"]["runtime"], (errno.EROFS, errno.EACCES))
                    self.assertEqual(observed["leaked_fds"], [])
                    self.assertIn(observed["fd_tables"]["1"], ("readable", "inaccessible"))
                    self.assertEqual(observed["fd_tables"][observed["self_pid"]], "readable")
                    self.assertIn(
                        observed["network"]["tcp"], (errno.ECONNREFUSED, errno.ENETUNREACH)
                    )
                    self.assertEqual(observed["network"]["abstract"], errno.ECONNREFUSED)
                    self.assertEqual(observed["network"]["pathname"], errno.ENOENT)
                self.assertEqual(result["netns"], child["netns"])
            self.assertEqual((home / "parent").read_text(), "session-parent")
            self.assertEqual((home / "child").read_text(), "session-child")
            self.assertEqual((project / "visible").read_text(), "authorized-material")
            self.assertEqual((project / ".git" / "HEAD").read_text(), "fictional-head")
            self.assertFalse((project / "new-file").exists())
            self.assertEqual(secret.read_text(), "fictional-private-key")
