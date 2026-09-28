from __future__ import annotations

import ipaddress
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class CodexRuntimeProxyHealth:
    ok: bool
    detail: str


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
