from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_PROXY_ENDPOINT = re.compile(
    rb"model_providers\.codex-multi-auth-runtime-proxy\.base_url="
    rb"[^\x00]*http://127\.0\.0\.1:(\d+)"
)


@dataclass(frozen=True, slots=True)
class CodexRuntimeProxyHealth:
    ok: bool
    detail: str


def probe_codex_runtime_proxy(
    *,
    proc_root: Path = Path("/proc"),
    connect: Callable[..., socket.socket] = socket.create_connection,
) -> CodexRuntimeProxyHealth:
    """Verify the dynamic multi-auth upstream, not merely its parent service."""
    ports: set[int] = set()
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return CodexRuntimeProxyHealth(False, "process table unavailable")
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        match = _PROXY_ENDPOINT.search(command)
        if match is not None:
            ports.add(int(match.group(1)))
    if not ports:
        return CodexRuntimeProxyHealth(False, "runtime proxy endpoint is not advertised")
    for port in sorted(ports):
        try:
            connection = connect(("127.0.0.1", port), timeout=1.0)
        except OSError:
            continue
        try:
            return CodexRuntimeProxyHealth(True, "runtime proxy listener is reachable")
        finally:
            connection.close()
    return CodexRuntimeProxyHealth(False, "runtime proxy endpoint is not reachable")
