"""Trusted test trampoline: verify a controlling terminal before native exec."""

from __future__ import annotations

import fcntl
import os
import struct
import sys
import termios


def main() -> None:
    evidence = int(sys.argv[1])
    os.set_inheritable(evidence, False)
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        if (
            not all(os.isatty(fd) for fd in (0, 1, 2))
            or len({os.fstat(fd).st_rdev for fd in (0, 1, 2)}) != 1
            or os.getsid(0) != os.getpid()
            or os.getpgrp() != os.getpid()
            or os.tcgetpgrp(0) != os.getpid()
            or struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))[:2] != (24, 80)
        ):
            raise RuntimeError("terminal verification failed")
        if os.write(evidence, b"PTY1\n") != 5:
            raise RuntimeError("terminal proof write failed")
    finally:
        os.close(evidence)
    os.execvpe(sys.argv[2], sys.argv[2:], dict(os.environ))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # No input, environment, path or terminal bytes enter a diagnostic.
        raise SystemExit(126) from None
