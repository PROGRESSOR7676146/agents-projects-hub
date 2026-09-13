from __future__ import annotations

import argparse
import socket
import time
from pathlib import Path


def wait_for_unix_socket(
    path: Path,
    *,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.1,
) -> bool:
    """Return only after a Unix stream socket accepts a connection."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(min(interval_seconds, max(deadline - time.monotonic(), 0.001)))
        try:
            client.connect(str(path))
            return True
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval_seconds, remaining))
        finally:
            client.close()
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="wait until a Unix stream socket accepts connections"
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        ready = wait_for_unix_socket(args.path, timeout_seconds=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if ready:
        return 0
    parser.exit(1, f"Unix socket did not become connectable: {args.path}\n")


if __name__ == "__main__":
    raise SystemExit(main())
