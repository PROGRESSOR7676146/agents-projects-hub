"""Linux Unix-socket peer identity check for protected local channels."""

from __future__ import annotations

import os
import socket
import struct
import sys


class PeerCredentialError(RuntimeError):
    """The connected Unix peer could not be trusted."""


_UCRED = struct.Struct("=iII")  # Linux struct ucred: pid_t, uid_t, gid_t.


def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    """Read the kernel credential payload; kept separate for fault injection."""
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise PeerCredentialError("untrusted Unix peer")
    payload = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    if not isinstance(payload, bytes) or len(payload) != _UCRED.size:
        raise PeerCredentialError("untrusted Unix peer")
    return _UCRED.unpack(payload)


def require_same_uid_peer(connection: socket.socket) -> None:
    """Fail closed unless a live peer has the effective worker UID."""
    try:
        pid, uid, gid = _peer_credentials(connection)
        if (
            type(pid) is not int
            or type(uid) is not int
            or type(gid) is not int
            or pid <= 0
            or uid != os.geteuid()
        ):
            raise PeerCredentialError("untrusted Unix peer")
    except (OSError, ValueError, TypeError, AttributeError, struct.error, PeerCredentialError):
        raise PeerCredentialError("untrusted Unix peer") from None
