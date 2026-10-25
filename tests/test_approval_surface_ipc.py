from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router import approval_surface as surface


class ApprovalSurfaceIpcTests(unittest.TestCase):
    def test_bounded_read_only_protocol_and_socket_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            home.chmod(0o700)
            path = home / "daemon.sock"
            info = os.stat_result(
                (stat.S_IFSOCK | 0o600, 42, 1, 1, os.getuid(), os.getgid(), 0, 0, 0, 0)
            )
            original = Path.lstat

            def lstat(selected):
                return info if selected == path else original(selected)

            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.recv.side_effect = [b'{"kind":"approval.snapshot","entries":[]}', b"\n"]
            with (
                patch.object(Path, "lstat", lstat),
                patch.object(surface.socket, "socket", return_value=client),
            ):
                self.assertEqual(
                    surface.read_snapshot(path), dict(kind="approval.snapshot", entries=[])
                )
            client.sendall.assert_called_once_with(b'{"kind":"approval.snapshot"}\n')
            for payload in (b"not-json\n", b"x" * (surface.MAX_SNAPSHOT_BYTES + 1), b""):
                client.recv.side_effect = [payload]
                with (
                    patch.object(Path, "lstat", lstat),
                    patch.object(surface.socket, "socket", return_value=client),
                ):
                    self.assertIsNone(surface.read_snapshot(path))
            count = 0

            def replaced(selected):
                nonlocal count
                if selected != path:
                    return original(selected)
                count += 1
                return (
                    info
                    if count == 1
                    else os.stat_result(
                        (stat.S_IFSOCK | 0o600, 43, 1, 1, os.getuid(), os.getgid(), 0, 0, 0, 0)
                    )
                )

            client.recv.side_effect = [json.dumps(dict(kind="approval.snapshot")).encode() + b"\n"]
            with (
                patch.object(Path, "lstat", replaced),
                patch.object(surface.socket, "socket", return_value=client),
            ):
                self.assertIsNone(surface.read_snapshot(path))

    def test_missing_public_parent_and_transport_fault_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            home.chmod(0o755)
            self.assertIsNone(surface.read_snapshot(home / "missing.sock"))
