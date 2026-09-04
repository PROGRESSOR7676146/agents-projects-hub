from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .codex_accounts import read_codex_pool_status

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


def probe_codex_config_proxy(
    config_path: Path | None = None,
    *,
    connect: Callable[..., Any] = socket.create_connection,
) -> CodexRuntimeProxyHealth:
    """Verify only a configured loopback proxy without exposing its URL."""
    path = config_path if config_path is not None else Path.home() / ".codex" / "config.toml"
    if not path.is_file():
        return CodexRuntimeProxyHealth(True, "Codex config is not present")
    try:
        if path.stat().st_size > 1_000_000:
            return CodexRuntimeProxyHealth(False, "Codex config is oversized")
        content = path.read_text("utf-8")
        data = tomllib.loads(content)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return CodexRuntimeProxyHealth(False, "Codex config cannot be read safely")

    provider = data.get("model_provider")
    if not isinstance(provider, str) or not provider.strip():
        return CodexRuntimeProxyHealth(True, "direct or default provider")

    providers = data.get("model_providers")
    if not isinstance(providers, dict):
        return CodexRuntimeProxyHealth(True, "selected provider has no configured proxy")

    provider_cfg = providers.get(provider)
    if not isinstance(provider_cfg, dict):
        return CodexRuntimeProxyHealth(True, "selected provider has no configured proxy")

    base_url = provider_cfg.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return CodexRuntimeProxyHealth(True, "selected provider has no configured proxy")

    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return CodexRuntimeProxyHealth(False, "configured provider URL is invalid")
    if parsed.scheme not in {"http", "https"} or host is None:
        return CodexRuntimeProxyHealth(False, "configured provider URL is invalid")
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.casefold() == "localhost"
    if not is_loopback:
        return CodexRuntimeProxyHealth(True, "selected provider does not use a loopback proxy")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        conn = connect((host, port), timeout=1.0)
        try:
            return CodexRuntimeProxyHealth(True, "configured loopback proxy is reachable")
        finally:
            conn.close()
    except OSError:
        return CodexRuntimeProxyHealth(
            False,
            "configured loopback proxy is unreachable; inspect the managed app-server "
            "before changing the Codex provider binding",
        )


def probe_codex_multi_auth_accounts(
    multi_auth_dir: Path,
    *,
    executable: str = "codex-multi-auth",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CodexRuntimeProxyHealth:
    """Read the supported redacted report and surface explicit invalidation."""
    status = read_codex_pool_status(multi_auth_dir, executable=executable, runner=runner)
    if not status.available:
        return CodexRuntimeProxyHealth(False, "Codex account-pool status is unavailable")
    invalid_accounts = [str(item.index) for item in status.accounts if item.auth_invalidated]
    if invalid_accounts:
        return CodexRuntimeProxyHealth(
            False,
            f"Codex account token invalidation detected for account(s) "
            f"{', '.join(invalid_accounts)}; re-authentication is required",
        )
    return CodexRuntimeProxyHealth(True, "configured Codex accounts have no invalidation marker")
