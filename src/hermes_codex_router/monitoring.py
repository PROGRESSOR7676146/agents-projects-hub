from __future__ import annotations

import html
import subprocess
from dataclasses import asdict
from typing import Any, Callable, Mapping

from .alerts import OperationalAlert, evaluate_operational_alerts
from .codex_accounts import read_codex_pool_status
from .diagnostics import run_doctor
from .hub_config import HubConfig
from .state import HubState
from .telegram import TelegramBotApi, TelegramError


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


def _send_hermes(
    target: str,
    message: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    completed = run(
        ("hermes", "send", "--to", target, "--quiet", "-"),
        input=message,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Hermes recovery-channel delivery failed")


def _telegram_access(config: HubConfig) -> dict[tuple[str, str], bool]:
    access: dict[tuple[str, str], bool] = {}
    for agent in config.agents:
        if agent.managed_externally or agent.token_file is None:
            continue
        telegram = TelegramBotApi(agent.token_file.read_text(encoding="utf-8").strip())
        for project in config.projects:
            if project.telegram_chat_id is None:
                continue
            try:
                result = telegram.call("getChat", chat_id=project.telegram_chat_id)
            except TelegramError:
                access[(agent.agent_id, project.project_id)] = False
            else:
                access[(agent.agent_id, project.project_id)] = isinstance(result, dict)
    return access


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
        doctor = run_doctor(config)
        raw_checks = doctor.get("checks")
        doctor_checks = raw_checks if isinstance(raw_checks, list) else []
        recovery_status = {
            str(check["name"]).split(":", 1)[1]: bool(check["ok"])
            for check in doctor_checks
            if isinstance(check, dict) and str(check.get("name", "")).startswith("recovery:")
        }
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot=snapshot,
            doctor_ok=bool(doctor["ok"]),
            recovery_status=recovery_status or None,
            telegram_access=_telegram_access(config),
        )
        delivered: list[str] = []
        if notify and alerts:
            destinations = _destinations(snapshot)
            codex_due = tuple(
                alert
                for alert in alerts
                if destinations
                and state.claim_alert_delivery(
                    f"{alert.key}:codex", cooldown_seconds=cooldown_seconds
                )
            )
            if codex_due and destinations:
                agent = config.require_agent("codex")
                if agent.token_file is None:
                    raise RuntimeError("managed Codex bot token is unavailable")
                telegram = TelegramBotApi(agent.token_file.read_text(encoding="utf-8").strip())
                rendered = _render(codex_due)
                try:
                    for chat_id, thread_id in destinations:
                        telegram.send_html(chat_id, thread_id, rendered[:4090])
                except Exception:
                    for alert in codex_due:
                        state.release_alert_delivery(f"{alert.key}:codex")
                    raise
                delivered.extend(f"{alert.code}:codex" for alert in codex_due)
            if config.recovery_plane.enabled:
                hermes_due = tuple(
                    alert
                    for alert in alerts
                    if state.claim_alert_delivery(
                        f"{alert.key}:hermes", cooldown_seconds=cooldown_seconds
                    )
                )
                if hermes_due:
                    try:
                        _send_hermes(
                            config.recovery_plane.hermes_notify_target,
                            html.unescape(_render(hermes_due)),
                        )
                    except Exception:
                        for alert in hermes_due:
                            state.release_alert_delivery(f"{alert.key}:hermes")
                        raise
                    delivered.extend(f"{alert.code}:hermes" for alert in hermes_due)
        return {
            "ok": not any(alert.severity == "error" for alert in alerts),
            "alerts": [asdict(alert) for alert in alerts],
            "delivered": delivered,
        }
    finally:
        state.close()
