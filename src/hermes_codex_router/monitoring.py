from __future__ import annotations

import html
from dataclasses import asdict
from typing import Mapping

from .alerts import OperationalAlert, evaluate_operational_alerts
from .codex_accounts import read_codex_pool_status
from .diagnostics import run_doctor
from .hub_config import HubConfig
from .state import HubState
from .telegram import TelegramBotApi


def _render(alerts: tuple[OperationalAlert, ...]) -> str:
    lines = ["Project Hub operational alert"]
    lines.extend(f"[{alert.severity.upper()}] {alert.message}" for alert in alerts)
    return html.escape("\n".join(lines))


def _destinations(snapshot: Mapping[str, object]) -> list[tuple[int, int]]:
    """Choose one stable operational-alert topic per Telegram chat."""
    destinations: list[tuple[int, int]] = []
    seen_chats: set[int] = set()
    topics = snapshot.get("topics")
    if not isinstance(topics, list):
        return destinations
    for row in topics:
        if not isinstance(row, dict):
            continue
        chat_id = row.get("chat_id")
        thread_id = row.get("thread_id")
        if not isinstance(chat_id, int) or not isinstance(thread_id, int):
            continue
        if chat_id in seen_chats:
            continue
        seen_chats.add(chat_id)
        destinations.append((chat_id, thread_id))
    return destinations


def run_monitor_once(
    config: HubConfig,
    *,
    notify: bool,
    cooldown_seconds: int = 60 * 60,
) -> dict[str, object]:
    state = HubState.open(config.state_path)
    try:
        snapshot = state.status_snapshot()
        pool = (
            read_codex_pool_status(
                config.codex_multi_auth_dir,
                executable=(
                    str(config.codex_multi_auth_executable)
                    if config.codex_multi_auth_executable
                    else "codex-multi-auth"
                ),
            )
            if config.codex_multi_auth_dir is not None
            else None
        )
        if pool is None:
            from .codex_accounts import CodexPoolStatus

            pool = CodexPoolStatus(False, False, (), None, 0, "not_configured")
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot=snapshot,
            doctor_ok=bool(run_doctor(config)["ok"]),
        )
        delivered: list[str] = []
        if notify and alerts:
            destinations = _destinations(snapshot)
            due = tuple(
                alert
                for alert in alerts
                if destinations
                and state.claim_alert_delivery(alert.key, cooldown_seconds=cooldown_seconds)
            )
            if due and destinations:
                agent = config.require_agent("codex")
                if agent.token_file is None:
                    raise RuntimeError("managed Codex bot token is unavailable")
                telegram = TelegramBotApi(agent.token_file.read_text(encoding="utf-8").strip())
                rendered = _render(due)
                try:
                    for chat_id, thread_id in destinations:
                        telegram.send_html(chat_id, thread_id, rendered[:4090])
                except Exception:
                    for alert in due:
                        state.release_alert_delivery(alert.key)
                    raise
                delivered = [alert.code for alert in due]
        return {
            "ok": not any(alert.severity == "error" for alert in alerts),
            "alerts": [asdict(alert) for alert in alerts],
            "delivered": delivered,
        }
    finally:
        state.close()
