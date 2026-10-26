from __future__ import annotations

import os
import socket
import stat
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
                clients, accepted = [], []
                try:
                    for _ in range(3):
                        client = socket.socket(socket.AF_UNIX)
                        clients.append(client)
                        client.connect(str(path))
                        accepted.append(listener.accept()[0])
                    rows = Path("/proc/net/unix").read_text()
                    self.assertGreaterEqual(sum(str(path) in row for row in rows.splitlines()), 4)
                    owner = custody.process_identity(os.getpid())
                    inode = custody.verify_listener(path, owner)
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
