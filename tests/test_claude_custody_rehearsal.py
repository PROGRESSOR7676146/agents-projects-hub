"""Fictional custody attacks; never an attestation of a deployed OS boundary."""

from __future__ import annotations

import array
import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig, FileToolSandboxError
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)

_ATTACK = r"""
import array, ctypes, json, os, pathlib, socket, sys
inputs = json.loads(sys.argv[1])
result = {'private_reads': {}, 'syscalls': {}, 'network': {}}
private = inputs['private']
for name, path in {
    'direct': private,
    'project_symlink': 'private-link',
    'self_root': '/proc/self/root' + private,
    'init_root': '/proc/1/root' + private,
    'cwd_parent': '/proc/self/cwd/../authority/key',
    'init_cwd_parent': '/proc/1/cwd/../authority/key',
    'inherited_fd': '/proc/self/fd/' + str(inputs['secret_fd']),
}.items():
    try:
        pathlib.Path(path).read_bytes()
    except OSError as error:
        result['private_reads'][name] = error.errno
    else:
        raise AssertionError('private authority readable: ' + name)
result['cap_eff'] = next(line.split()[1] for line in
    pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
visible_fd = os.open('visible', os.O_RDONLY)
try:
    result['proc_alias_control'] = pathlib.Path('/proc/self/fd/' + str(visible_fd)).read_text()
finally:
    os.close(visible_fd)
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                       ctypes.c_ulong, ctypes.c_void_p]
libc.unshare.argtypes = [ctypes.c_int]
libc.setns.argtypes = [ctypes.c_int, ctypes.c_int]
libc.chroot.argtypes = [ctypes.c_char_p]
ns_fd = os.open('/proc/self/ns/mnt', os.O_RDONLY)
try:
    operations = {
        'new_userns': lambda: libc.unshare(0x10000000),
        'mount_tmpfs': lambda: libc.mount(b'tmpfs', b'/tmp', b'tmpfs', 0, None),
        'remount_git': lambda: libc.mount(None, b'.git', None, 32 | 4096, None),
        'join_mountns': lambda: libc.setns(ns_fd, 0),
        'chroot': lambda: libc.chroot(b'/'),
    }
    for name, operation in operations.items():
        ctypes.set_errno(0)
        if operation() != -1:
            raise AssertionError('privileged operation succeeded: ' + name)
        result['syscalls'][name] = ctypes.get_errno()
finally:
    os.close(ns_fd)
for name, path in {'git': '.git/HEAD', 'runtime': sys.executable}.items():
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as error:
        result[name + '_write'] = error.errno
    else:
        os.close(fd)
        raise AssertionError('readonly source writable: ' + name)
for name, family, endpoint in (
    ('tcp_loopback', socket.AF_INET, ('127.0.0.1', inputs['port'])),
    ('abstract_unix', socket.AF_UNIX, '\0' + inputs['abstract']),
    ('private_unix', socket.AF_UNIX, inputs['private_socket']),
):
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        try:
            connection.connect(endpoint)
        except OSError as error:
            result['network'][name] = error.errno
        else:
            result['network'][name] = 'reachable'
            if name == 'abstract_unix':
                data, ancillary, flags, sender = connection.recvmsg(
                    1, socket.CMSG_SPACE(array.array('i').itemsize))
                descriptors = array.array('i')
                for level, kind, payload in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        descriptors.frombytes(payload)
                if data != b'x' or len(descriptors) != 1:
                    raise AssertionError('descriptor-transfer control failed')
                try:
                    result['abstract_fd'] = os.read(descriptors[0], 128).decode()
                finally:
                    for descriptor in descriptors:
                        os.close(descriptor)
pathlib.Path('allowed-write').write_text('project-write')
pathlib.Path('/home/example/allowed-write').write_text('session-write')
result['project'] = pathlib.Path('visible').read_text()
print(json.dumps(result))
"""


class ClaudeCustodyRehearsalTests(unittest.TestCase):
    def test_namespace_blocks_authority_aliases_and_privilege_but_shares_network(self) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        runtime = (
            python,
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not all(path.exists() for path in runtime):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        with tempfile.TemporaryDirectory(prefix="example-custody-") as directory:
            base = Path(directory)
            project, home, authority = (base / name for name in ("project", "session", "authority"))
            for path in (project, home, authority):
                path.mkdir(mode=0o700)
            (project / ".git").mkdir()
            (project / ".git" / "HEAD").write_text("fictional-git-head", encoding="utf-8")
            (project / "visible").write_text("project-readable", encoding="utf-8")
            secret = authority / "key"
            secret.write_text("fictional-private-key", encoding="utf-8")
            secret.chmod(0o600)
            (project / "private-link").symlink_to(secret)
            # Positive control: mode 0600 does not protect against an unconfined
            # process with this UID. The same bytes must disappear in the namespace.
            control = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                    str(secret),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                close_fds=True,
            )
            self.assertEqual(control.returncode, 0, control.stderr)
            self.assertEqual(control.stdout.strip(), "fictional-private-key")
            try:
                tcp_listener = socket.socket(socket.AF_INET)
            except PermissionError:
                namespace_unavailable(self, "local socket fixture unavailable")
            with (
                socket.socket(socket.AF_UNIX) as permission,
                socket.socket(socket.AF_UNIX) as private_socket,
                socket.socket(socket.AF_UNIX) as abstract,
                tcp_listener as tcp,
                secret.open("rb") as secret_handle,
            ):
                endpoint = base / "permission.sock"
                abstract_name = "example-custody-" + uuid4().hex
                private_endpoint = authority / "private.sock"
                try:
                    permission.bind(str(endpoint))
                    private_socket.bind(str(private_endpoint))
                    abstract.bind("\0" + abstract_name)
                    tcp.bind(("127.0.0.1", 0))
                except PermissionError:
                    namespace_unavailable(self, "local socket fixture unavailable")
                endpoint.chmod(0o600)
                for listener in (permission, private_socket, abstract, tcp):
                    listener.listen(1)
                try:
                    config = FileToolSandboxConfig(
                        bwrap_executable=bwrap,
                        project_root=project,
                        provider_home=home,
                        runtime_roots=runtime,
                        claude_executable=python,
                        python_executable=python,
                        hook_code_root=Path("/usr/lib/python3.12"),
                        permission_socket=endpoint,
                        private_paths=(authority,),
                    )
                except FileToolSandboxError:
                    namespace_unavailable(self, "system Python runtime is not immutable to worker")
                os.set_inheritable(secret_handle.fileno(), True)
                inputs = {
                    "private": str(secret),
                    "secret_fd": secret_handle.fileno(),
                    "port": tcp.getsockname()[1],
                    "abstract": abstract_name,
                    "private_socket": str(private_endpoint),
                }
                with config.wrap(
                    [str(python), "-I", "-c", _ATTACK, json.dumps(inputs)], {}, project
                ) as launch:
                    self.assertNotIn(secret_handle.fileno(), launch.pass_fds)
                    errors: list[Exception] = []
                    abstract.settimeout(5)

                    def transfer_descriptor() -> None:
                        try:
                            connection, _ = abstract.accept()
                            with connection:
                                connection.settimeout(2)
                                connection.sendmsg(
                                    [b"x"],
                                    [
                                        (
                                            socket.SOL_SOCKET,
                                            socket.SCM_RIGHTS,
                                            array.array("i", [secret_handle.fileno()]),
                                        )
                                    ],
                                )
                        except OSError as error:
                            errors.append(error)

                    thread = threading.Thread(target=transfer_descriptor, daemon=True)
                    thread.start()
                    try:
                        run = subprocess.run(
                            launch.argv,
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
                        thread.join(6)
                        self.assertFalse(thread.is_alive(), "descriptor fixture did not stop")
                if run.returncode and namespace_permission_refused(run.stderr):
                    namespace_unavailable(self, "kernel disallows user namespaces")
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(errors, [])
                result = json.loads(run.stdout)
                self.assertEqual(
                    set(result["private_reads"]),
                    {
                        "direct",
                        "project_symlink",
                        "self_root",
                        "init_root",
                        "cwd_parent",
                        "init_cwd_parent",
                        "inherited_fd",
                    },
                )
                self.assertTrue(
                    all(
                        value in (errno.ENOENT, errno.EACCES)
                        for value in result["private_reads"].values()
                    )
                )
                self.assertEqual(int(result["cap_eff"], 16), 0)
                calls = result["syscalls"]
                # Bubblewrap disables further user namespaces by exhausting the
                # namespace limit; LSM/kernel policy may instead return EPERM.
                self.assertIn(calls.pop("new_userns"), (errno.EPERM, errno.ENOSPC))
                self.assertEqual(
                    calls,
                    {
                        "mount_tmpfs": errno.EPERM,
                        "remount_git": errno.EPERM,
                        "join_mountns": errno.EPERM,
                        "chroot": errno.EPERM,
                    },
                )
                self.assertEqual(result["git_write"], errno.EROFS)
                self.assertIn(result["runtime_write"], (errno.EROFS, errno.EACCES))
                self.assertEqual(
                    result["network"],
                    {
                        "tcp_loopback": "reachable",
                        "abstract_unix": "reachable",
                        "private_unix": errno.ENOENT,
                    },
                )
                self.assertEqual(result["project"], "project-readable")
                self.assertEqual(result["proc_alias_control"], "project-readable")
                # Shared-network services can deliberately hand a hidden inode
                # into this namespace. Custody requires excluding such services.
                self.assertEqual(result["abstract_fd"], "fictional-private-key")
            self.assertEqual((project / "allowed-write").read_text(), "project-write")
            self.assertEqual((home / "allowed-write").read_text(), "session-write")
            self.assertEqual((project / ".git" / "HEAD").read_text(), "fictional-git-head")
            self.assertEqual(secret.read_text(), "fictional-private-key")
