"""Bounded passive Linux SOCK_DIAG evidence; text /proc names are not authority."""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Listener:
    kernel_inode: int
    vfs_inode: int
    vfs_device: int


def parse_listener(data: bytes, path: Path) -> Listener | None:
    if len(data) < 16:
        raise ValueError("truncated Unix diagnostic")
    family, kind, state, _, inode, _, _ = struct.unpack_from("=BBBBIII", data)
    if family != socket.AF_UNIX:
        raise ValueError("unexpected diagnostic family")
    attributes: dict[int, bytes] = {}
    offset = 16
    while offset < len(data):
        if len(data) - offset < 4:
            raise ValueError("truncated diagnostic attribute")
        length, tag = struct.unpack_from("=HH", data, offset)
        if length < 4 or offset + length > len(data) or tag in attributes:
            raise ValueError("invalid or duplicate diagnostic attribute")
        attributes[tag] = data[offset + 4 : offset + length]
        offset += (length + 3) & ~3
    if offset != len(data):
        raise ValueError("truncated attribute padding")
    if attributes.get(0) != os.fsencode(path) + b"\0":
        return None
    if kind != socket.SOCK_STREAM or state != 10 or inode <= 0:
        raise ValueError("expected path is not a stream listener")
    vfs = attributes.get(1, b"")
    if len(vfs) != 8:
        raise ValueError("listener filesystem evidence unavailable")
    vfs_inode, vfs_device = struct.unpack("=II", vfs)
    return Listener(inode, vfs_inode, vfs_device)


def require_vfs(listener: Listener, info: os.stat_result) -> None:
    # unix_diag_vfs uses kernel dev_t (12 major / 20 minor bits), not libc dev_t.
    major, minor = os.major(info.st_dev), os.minor(info.st_dev)
    encoded = (major << 20) | minor
    if listener.vfs_inode != info.st_ino or listener.vfs_device != encoded:
        raise ValueError("listener does not match current filesystem socket")


def query_listener(path: Path) -> Listener:
    """Read one complete, bounded kernel dump in the caller's network namespace."""
    sequence = int.from_bytes(os.urandom(4), "little")
    request = struct.pack("=BBHIIIII", socket.AF_UNIX, 0, 0, 1 << 10, 0, 3, 0xFFFFFFFF, 0xFFFFFFFF)
    header = struct.pack("=IHHII", 16 + len(request), 20, 0x301, sequence, 0)
    matches = []
    deadline = time.monotonic() + 2
    budget = 4 * 1024 * 1024
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as channel:
        channel.settimeout(2)
        channel.sendto(header + request, (0, 0))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or budget <= 0:
                raise ValueError("Unix diagnostic budget exhausted")
            channel.settimeout(remaining)
            data, _, flags, sender = channel.recvmsg(min(65536, budget))
            budget -= len(data)
            if not data or flags & socket.MSG_TRUNC or sender[0] != 0:
                raise ValueError("truncated or non-kernel diagnostic")
            offset = 0
            while offset < len(data):
                if len(data) - offset < 16:
                    raise ValueError("truncated netlink header")
                length, kind, message_flags, seq, _ = struct.unpack_from("=IHHII", data, offset)
                if length < 16 or offset + length > len(data) or seq != sequence:
                    raise ValueError("invalid diagnostic envelope")
                if message_flags & 0x10:
                    raise ValueError("interrupted diagnostic dump")
                payload = data[offset + 16 : offset + length]
                offset += (length + 3) & ~3
                if offset > len(data):
                    raise ValueError("truncated netlink padding")
                if kind == 3:
                    if payload and (len(payload) < 4 or struct.unpack_from("=i", payload)[0]):
                        raise ValueError("failed diagnostic dump")
                    if offset != len(data) or len(matches) != 1:
                        raise ValueError("missing or ambiguous Unix stream listener")
                    return matches[0]
                if kind != 20:
                    raise ValueError("failed or unexpected diagnostic message")
                listener = parse_listener(payload, path)
                if listener is not None:
                    matches.append(listener)
