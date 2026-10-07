"""Require explicit case-sensitive lookup semantics for pinned Linux directories.

Dentry spelling is not on-disk-name evidence on casefold filesystems. This
validation grants no mount or invocation authority and owns only temporary FDs.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
import sys

_EXT = 0xEF53
_TMPFS = 0x01021994
_BTRFS = 0x9123683E
_XFS = 0x58465342
_CASEFOLD = 0x40000000
_XFS_ASCII_CI = 1 << 12


class LookupEvidenceError(ValueError):
    """Directory lookup semantics are unavailable or unsupported."""


class _StatFs(ctypes.Structure):
    # Linux 64-bit libc statfs ABI; unsupported word sizes fail before the call.
    _fields_ = [
        ("kind", ctypes.c_long),
        ("block_size", ctypes.c_long),
        ("counts", ctypes.c_ulong * 5),
        ("fsid", ctypes.c_int * 2),
        ("name_length", ctypes.c_long),
        ("fragment_size", ctypes.c_long),
        ("flags", ctypes.c_long),
        ("spare", ctypes.c_long * 4),
    ]


def _filesystem_type(fd: int) -> int:
    if (
        sys.platform != "linux"
        or ctypes.sizeof(ctypes.c_long) != 8
        or os.uname().machine not in {"x86_64", "aarch64"}
        or ctypes.sizeof(_StatFs) != 120
    ):
        raise LookupEvidenceError("directory lookup ABI is unsupported")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        inspect = libc.fstatfs
    except AttributeError as error:
        raise LookupEvidenceError("directory filesystem evidence is unavailable") from error
    inspect.argtypes = [ctypes.c_int, ctypes.POINTER(_StatFs)]
    inspect.restype = ctypes.c_int
    evidence = _StatFs()
    if inspect(fd, ctypes.byref(evidence)) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return evidence.kind & 0xFFFFFFFF


def _directory_flags(fd: int) -> int:
    command = (2 << 30) | (ctypes.sizeof(ctypes.c_long) << 16) | (ord("f") << 8) | 1
    data = fcntl.ioctl(fd, command, bytes(4))
    if not isinstance(data, bytes) or len(data) != 4:
        raise LookupEvidenceError("directory flag evidence is malformed")
    return struct.unpack("=I", data)[0]


def _xfs_geometry_flags(fd: int) -> int:
    # Linux xfs_fsop_geom_v1: 112 bytes on this supported 64-bit ABI;
    # flags is the uint32 at offset92, XFS_IOC_FSGEOMETRY_V1 uses _IOR('X',100).
    command = (2 << 30) | (112 << 16) | (ord("X") << 8) | 100
    data = fcntl.ioctl(fd, command, bytes(112))
    if not isinstance(data, bytes) or len(data) != 112:
        raise LookupEvidenceError("XFS geometry evidence is malformed")
    # This V1 ioctl reports XFS_FSOP_GEOM_VERSION (0), even on V5 filesystems.
    if struct.unpack_from("=i", data, 88)[0] != 0:
        raise LookupEvidenceError("XFS geometry version is unsupported")
    return struct.unpack_from("=I", data, 92)[0]


def require_case_sensitive_directory(fd: int) -> None:
    """Inspect pinned '.', never reinterpret an error as absent casefolding."""
    descriptor = -1
    try:
        descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd)
        filesystem = _filesystem_type(descriptor)
        if filesystem in {_EXT, _TMPFS, _BTRFS}:
            if _directory_flags(descriptor) & _CASEFOLD:
                raise LookupEvidenceError("casefold directory lookups are unsupported")
        elif filesystem == _XFS:
            if _xfs_geometry_flags(descriptor) & _XFS_ASCII_CI:
                raise LookupEvidenceError("XFS insensitive directory lookups are unsupported")
        else:
            raise LookupEvidenceError("directory filesystem lookup semantics are unsupported")
    except OSError as error:
        raise LookupEvidenceError("directory lookup evidence is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
