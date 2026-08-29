from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .telegram import TelegramBotApi, TelegramError

HERMES_POLICY_KEYS = (
    "platforms.telegram.allowed_chats",
    "platforms.telegram.group_allowed_chats",
)


@dataclass(frozen=True, slots=True)
class HermesGroupPolicy:
    ok: bool
    missing_allowed_chats: tuple[int, ...]
    missing_group_allowed_chats: tuple[int, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class HermesGatewayHeartbeat:
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class HermesBotApiHealth:
    ok: bool
    pending_updates: int | None
    detail: str


def _run_config_get(
    key: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[int, ...] | None:
    try:
        completed = run(
            ("hermes", "config", "get", key, "--json"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
        return None
    return tuple(value)


def probe_hermes_group_policy(
    expected_chat_ids: tuple[int, ...],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> HermesGroupPolicy:
    expected = set(expected_chat_ids)
    allowed = _run_config_get(HERMES_POLICY_KEYS[0], run=run)
    group_allowed = _run_config_get(HERMES_POLICY_KEYS[1], run=run)
    missing_allowed = tuple(sorted(expected - set(allowed or ())))
    missing_group = tuple(sorted(expected - set(group_allowed or ())))
    readable = allowed is not None and group_allowed is not None
    ok = readable and not missing_allowed and not missing_group
    if not readable:
        detail = "Hermes Telegram group policy could not be read"
    elif ok:
        detail = f"all {len(expected)} project groups allowed"
    else:
        detail = (
            f"missing allowed_chats={list(missing_allowed)}; "
            f"missing group_allowed_chats={list(missing_group)}"
        )
    return HermesGroupPolicy(ok, missing_allowed, missing_group, detail)


def sync_hermes_group_policy(
    expected_chat_ids: tuple[int, ...],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> bool:
    expected = set(expected_chat_ids)
    current = {key: _run_config_get(key, run=run) for key in HERMES_POLICY_KEYS}
    if any(value is None for value in current.values()):
        raise RuntimeError("Hermes Telegram group policy could not be read safely")
    changed = False
    for key in HERMES_POLICY_KEYS:
        existing = current[key]
        assert existing is not None
        merged = sorted(set(existing) | expected)
        if tuple(merged) == existing:
            continue
        completed = run(
            ("hermes", "config", "set", key, json.dumps(merged, separators=(",", ":"))),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Hermes config update failed for {key}")
        changed = True
    return changed


def probe_gateway_heartbeat(
    path: Path,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 180,
) -> HermesGatewayHeartbeat:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        updated_at = datetime.fromisoformat(str(document["updated_at"]))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        socket_tick = document.get("loop_tick_socket") is True
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return HermesGatewayHeartbeat(False, "heartbeat missing or invalid")
    evaluated_at = now or datetime.now(timezone.utc)
    age = max(0.0, (evaluated_at - updated_at).total_seconds())
    ok = socket_tick and age <= stale_after_seconds
    return HermesGatewayHeartbeat(
        ok,
        f"age={int(age)}s, loop_tick_socket={'ok' if socket_tick else 'failed'}",
    )


def _dotenv_value(path: Path, name: str) -> str | None:
    try:
        mode = path.stat().st_mode
        if not path.is_file() or mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def probe_hermes_bot_api(
    env_path: Path,
    *,
    api_factory: Callable[[str], Any] = TelegramBotApi,
) -> HermesBotApiHealth:
    token = _dotenv_value(env_path, "TELEGRAM_BOT_TOKEN")
    if token is None:
        return HermesBotApiHealth(False, None, "private Telegram token unavailable")
    try:
        result = api_factory(token).call("getWebhookInfo")
    except (TelegramError, OSError, TimeoutError):
        return HermesBotApiHealth(False, None, "Telegram Bot API probe failed")
    if not isinstance(result, dict):
        return HermesBotApiHealth(False, None, "Telegram Bot API returned invalid status")
    pending = result.get("pending_update_count")
    if not isinstance(pending, int) or pending < 0:
        return HermesBotApiHealth(False, None, "Telegram pending-update count unavailable")
    return HermesBotApiHealth(True, pending, f"pending_updates={pending}")


def restart_hermes_gateway(
    service_unit: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    completed = run(
        ("systemctl", "--user", "restart", service_unit),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Hermes Gateway restart failed")
