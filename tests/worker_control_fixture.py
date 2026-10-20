"""Disposable process for kernel peer/PID-replacement tests, never a service."""

from __future__ import annotations

import sys
from pathlib import Path

from hermes_codex_router.release_identity import ReleaseIdentity
from hermes_codex_router.worker_activity_barrier import WorkerActivityBarrier
from hermes_codex_router.worker_control_socket import WorkerControlEndpoint
from hermes_codex_router.worker_drain_protocol import current_binding


def main() -> None:
    release = ReleaseIdentity("0.0.0", "a" * 40, "2026-10-01T00:00:00+00:00", True)
    endpoint = WorkerControlEndpoint(
        Path(sys.argv[1]),
        current_binding("example-worker", "b" * 32, release=release),
        WorkerActivityBarrier(),
    )
    try:
        endpoint.start()
        print("ready", flush=True)
        sys.stdin.readline()
    finally:
        endpoint.close()


if __name__ == "__main__":
    main()
