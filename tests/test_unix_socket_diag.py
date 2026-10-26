from __future__ import annotations

import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import unix_socket_diag as diag


class UnixSocketDiagTests(unittest.TestCase):
    def message(self, name=b"/example.sock\0", *, inode=123, vfs_inode=456):
        def attr(kind, data):
            value = struct.pack("=HH", len(data) + 4, kind) + data
            return value + bytes((-len(value)) % 4)

        return struct.pack("=BBBBIII", socket.AF_UNIX, socket.SOCK_STREAM, 10, 0, inode, 0, 0) + (
            attr(0, name) + attr(1, struct.pack("=II", vfs_inode, 0x800030))
        )

    def test_binary_name_cannot_inject_listener_row(self):
        fake_row = b"\0\n000: 2 0 00010000 0001 01 123 /example.sock"
        self.assertIsNone(diag.parse_listener(self.message(fake_row), Path("/example.sock")))
        result = diag.parse_listener(self.message(), Path("/example.sock"))
        self.assertEqual(result, diag.Listener(123, 456, 0x800030))

    def test_truncated_duplicate_and_missing_vfs_refused(self):
        message = self.message()
        for data in (message[:8], message[:-5], message + message[16:], message[:-12]):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                diag.parse_listener(data, Path("/example.sock"))

    def test_real_listener_matches_filesystem_and_ignores_abstract_newline_name(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.sock"
            with (
                socket.socket(socket.AF_UNIX) as listener,
                socket.socket(socket.AF_UNIX) as hostile,
            ):
                listener.bind(str(path))
                listener.listen(1)
                hostile.bind(b"\0\n000: 2 0 00010000 0001 01 123 " + os.fsencode(path))
                hostile.listen(1)
                observed = diag.query_listener(path)
                self.assertEqual(
                    os.readlink(f"/proc/self/fd/{listener.fileno()}"),
                    f"socket:[{observed.kernel_inode}]",
                )
                diag.require_vfs(observed, path.lstat())
                old = observed
                path.unlink()
                with socket.socket(socket.AF_UNIX) as replacement:
                    replacement.bind(str(path))
                    replacement.listen(1)
                    with self.assertRaises(ValueError):
                        diag.require_vfs(old, path.lstat())
                    with self.assertRaisesRegex(ValueError, "ambiguous"):
                        diag.query_listener(path)

    def test_netlink_envelope_failures_and_duplicate_listeners_refuse(self):
        def packet(kind, body=b"", flags=0, seq=0):
            value = struct.pack("=IHHII", 16 + len(body), kind, flags, seq, 0) + body
            return value + bytes((-len(value)) % 4)

        row = packet(20, self.message())
        done = packet(3, bytes(4))
        cases = (
            (row + done, socket.MSG_TRUNC, (0, 0)),
            (row + done, 0, (123, 0)),
            (packet(20, self.message(), seq=1), 0, (0, 0)),
            (packet(3, bytes(4), flags=0x10), 0, (0, 0)),
            (packet(2, struct.pack("=i", -1)), 0, (0, 0)),
            (row + row + done, 0, (0, 0)),
            (done, 0, (0, 0)),
            (row[:15], 0, (0, 0)),
        )
        for data, flags, sender in cases:
            with self.subTest(data=data[:16], flags=flags, sender=sender):
                with (
                    patch.object(diag.socket, "socket") as factory,
                    patch.object(diag.os, "urandom", return_value=bytes(4)),
                ):
                    channel = factory.return_value.__enter__.return_value
                    channel.recvmsg.return_value = (data, [], flags, sender)
                    with self.assertRaises(ValueError):
                        diag.query_listener(Path("/example.sock"))
