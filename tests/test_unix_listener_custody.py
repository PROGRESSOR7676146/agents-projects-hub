from __future__ import annotations

import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import unix_listener_custody as custody


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

    def test_missing_duplicate_wrong_type_and_non_listening_rows_fail_closed(self):
        row = "000: 2 0 00010000 0001 01 123 /example.sock"
        self.assertEqual(custody.listener_inode(row, Path("/example.sock")), 123)
        for rows in (
            "",
            row + "\n" + row.replace("123", "456"),
            row.replace("0001 01", "0002 01"),
            row.replace("00010000", "00000000"),
            row.replace("0001 01", "0001 03"),
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                custody.listener_inode(rows, Path("/example.sock"))

    def test_wrong_fd_owner_and_changed_incarnation_or_listener_refused(self):
        owner = custody.ProcessIdentity(123, "example-boot", 10, "/example/codex", "example", 1000)
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            (proc / "123/fd").mkdir(parents=True)
            (proc / "net").mkdir()
            row = "000: 2 0 00010000 0001 01 123 /example.sock"
            (proc / "net/unix").write_text(row)
            with patch.object(custody, "process_identity", return_value=owner):
                with self.assertRaisesRegex(ValueError, "descriptor"):
                    custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)
            (proc / "123/fd/4").symlink_to("socket:[123]")
            with patch.object(
                custody, "process_identity", side_effect=[owner, replace(owner, boot_id="new-boot")]
            ):
                with self.assertRaisesRegex(ValueError, "incarnation"):
                    custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)
            with (
                patch.object(custody, "process_identity", return_value=owner),
                patch.object(custody, "listener_inode", side_effect=[123, 456]),
                self.assertRaisesRegex(ValueError, "listener changed"),
            ):
                custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)

    def test_owner_closes_last_fd_between_passes_while_listener_remains(self):
        owner = custody.ProcessIdentity(123, "example-boot", 10, "/example/codex", "example", 1000)
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            (proc / "123/fd").mkdir(parents=True)
            descriptor = proc / "123/fd/4"
            descriptor.symlink_to("socket:[123]")
            (proc / "net").mkdir()
            (proc / "net/unix").write_text("000: 2 0 00010000 0001 01 123 /example.sock")
            calls = 0

            def identity(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    descriptor.unlink()  # Another holder keeps the same listener alive.
                return owner

            with patch.object(custody, "process_identity", side_effect=identity):
                with self.assertRaisesRegex(ValueError, "descriptor"):
                    custody.verify_listener(Path("/example.sock"), owner, proc_root=proc)
