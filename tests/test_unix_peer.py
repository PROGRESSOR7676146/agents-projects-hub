import errno
import os
import socket
import struct
import unittest
from unittest.mock import patch

from hermes_codex_router import unix_peer


class UnixPeerTests(unittest.TestCase):
    def test_real_same_uid_socket_passes(self):
        if not hasattr(socket, "SO_PEERCRED"):
            self.skipTest("SO_PEERCRED unavailable")
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            try:
                left.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("=iII"))
            except OSError as exc:
                if exc.errno in (errno.EPERM, errno.EACCES):
                    self.skipTest("sandbox blocks peer credential reads")
                raise
            unix_peer.require_same_uid_peer(left)
            unix_peer.require_same_uid_peer(right)

    def test_wrong_uid_unavailable_and_invalid_pid_fail_closed(self):
        cases = ((1, os.geteuid() + 1, 0), (0, os.geteuid(), 0))
        for credentials in cases:
            with self.subTest(credentials=credentials):
                with patch.object(unix_peer, "_peer_credentials", return_value=credentials):
                    with self.assertRaises(unix_peer.PeerCredentialError):
                        unix_peer.require_same_uid_peer(object())
        with patch.object(unix_peer, "_peer_credentials", return_value=(1, os.geteuid(), 12345)):
            unix_peer.require_same_uid_peer(object())
        with patch.object(unix_peer, "_peer_credentials", side_effect=OSError("secret path")):
            with self.assertRaisesRegex(
                unix_peer.PeerCredentialError, "untrusted Unix peer"
            ) as error:
                unix_peer.require_same_uid_peer(object())
        self.assertNotIn("secret path", str(error.exception))

    def test_kernel_payload_must_be_exact_size(self):
        class FakeSocket:
            def __init__(self, payload):
                self.payload = payload

            def getsockopt(self, level, option, size):
                assert level == socket.SOL_SOCKET
                assert option == socket.SO_PEERCRED
                assert size == struct.calcsize("=iII")
                return self.payload

        for payload in (b"", b"\0" * 11, b"\0" * 13):
            with self.subTest(size=len(payload)):
                with self.assertRaises(unix_peer.PeerCredentialError):
                    unix_peer.require_same_uid_peer(FakeSocket(payload))

    def test_non_linux_platform_fails_closed(self):
        with patch.object(unix_peer.sys, "platform", "darwin"):
            with self.assertRaises(unix_peer.PeerCredentialError):
                unix_peer.require_same_uid_peer(object())


if __name__ == "__main__":
    unittest.main()
