from __future__ import annotations

import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import unix_listener_custody as custody
from hermes_codex_router.unix_socket_diag import Listener


class UnixListenerCustodyTests(unittest.TestCase):
    def test_real_listener_with_multiple_accepted_connections(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.sock"
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
                listener.listen(4)
                listener.settimeout(2)
                clients, accepted = [], []
                try:
                    owner = custody.process_identity(os.getpid())
                    inode = custody.verify_listener(path, owner)
                    self.assertEqual(
                        listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1
                    )
                    for _ in range(3):
                        client = socket.socket(socket.AF_UNIX)
                        client.settimeout(2)
                        clients.append(client)
                        client.connect(str(path))
                        accepted.append(listener.accept()[0])
                        peer_pid, peer_uid, _ = struct.unpack(
                            "=iII", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        )
                        self.assertEqual((peer_pid, peer_uid), (owner.pid, owner.uid))
                        self.assertEqual(custody.verify_listener(path, owner), inode)
                    rows = Path("/proc/net/unix").read_text()
                    self.assertGreaterEqual(sum(str(path) in row for row in rows.splitlines()), 4)
                    self.assertEqual(
                        os.readlink(f"/proc/self/fd/{listener.fileno()}"), f"socket:[{inode}]"
                    )
                    with self.assertRaises(ValueError):
                        custody.verify_listener(
                            path, replace(owner, start_ticks=owner.start_ticks + 1)
                        )
                finally:
                    for connection in clients + accepted:
                        connection.close()

    def test_real_listener_refuses_foreign_pid_even_with_matching_uid(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as child,
            socket.socket(socket.AF_UNIX) as listener,
        ):
            path = Path(temp) / "native.sock"
            listener.bind(str(path))
            listener.listen(1)
            try:
                foreign = custody.process_identity(child.pid)
                self.assertEqual(foreign.uid, os.getuid())
                with self.assertRaisesRegex(ValueError, "descriptor"):
                    custody.verify_listener(path, foreign)
                custody.verify_listener(path, custody.process_identity(os.getpid()))
            finally:
                child.communicate(b"x", timeout=10)

    def test_real_closed_listener_and_rebound_path_refuse(self):
        with tempfile.TemporaryDirectory() as temp, socket.socket(socket.AF_UNIX) as original:
            path = Path(temp) / "native.sock"
            owner = custody.process_identity(os.getpid())
            original.bind(str(path))
            original.listen(1)
            custody.verify_listener(path, owner)
            path.unlink()
            with socket.socket(socket.AF_UNIX) as replacement:
                replacement.bind(str(path))
                replacement.listen(1)
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    custody.verify_listener(path, owner)
            original.close()
            self.assertTrue(path.exists())  # A leftover pathname proves no listener.
            with self.assertRaisesRegex(ValueError, "missing"):
                custody.verify_listener(path, owner)

    def fixture(self, stack):
        temp = stack.enter_context(tempfile.TemporaryDirectory())
        proc = Path(temp)
        (proc / "123/fd").mkdir(parents=True)
        owner = custody.ProcessIdentity(123, "example-boot", 10, "/example/codex", "example", 1000)
        info = os.stat_result(
            (stat.S_IFSOCK | 0o600, 456, os.makedev(8, 48), 1, 1000, 1000, 0, 0, 0, 0)
        )
        stack.enter_context(patch.object(custody, "_socket_identity", return_value=info))
        stack.enter_context(patch.object(custody, "_same_network_namespace"))
        query = stack.enter_context(
            patch.object(custody, "query_listener", return_value=Listener(123, 456, 0x800030))
        )
        identity = stack.enter_context(
            patch.object(custody, "process_identity", return_value=owner)
        )
        return proc, owner, query, identity

    def test_wrong_fd_owner_and_changed_incarnation_or_listener_refused(self):
        with ExitStack() as stack:
            proc, owner, query, identity = self.fixture(stack)
            with self.assertRaisesRegex(ValueError, "descriptor"):
                custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)
            (proc / "123/fd/4").symlink_to("socket:[123]")
            identity.side_effect = [owner, replace(owner, boot_id="new-boot")]
            with self.assertRaisesRegex(ValueError, "incarnation"):
                custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)
            identity.side_effect = None
            query.side_effect = [Listener(123, 456, 0x800030), Listener(789, 456, 0x800030)]
            with self.assertRaisesRegex(ValueError, "listener changed"):
                custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)

    def test_owner_closes_last_fd_between_passes_while_listener_remains(self):
        with ExitStack() as stack:
            proc, owner, _, identity_mock = self.fixture(stack)
            descriptor = proc / "123/fd/4"
            descriptor.symlink_to("socket:[123]")
            calls = 0

            def identity(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    descriptor.unlink()
                return owner

            identity_mock.side_effect = identity
            with self.assertRaisesRegex(ValueError, "descriptor"):
                custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)

    def test_missing_kernel_evidence_and_foreign_namespace_refuse_without_effects(self):
        with ExitStack() as stack:
            proc, owner, query, _ = self.fixture(stack)
            query.side_effect = PermissionError("SOCK_DIAG refused")
            with self.assertRaises(PermissionError):
                custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)
        with patch.object(custody.os, "readlink", side_effect=["net:[1]", "net:[2]"]):
            with self.assertRaisesRegex(ValueError, "network namespace"):
                custody._same_network_namespace(Path("/proc"), 123)
