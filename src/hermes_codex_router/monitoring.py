from __future__ import annotations

import html
import subprocess
import time
from dataclasses import asdict
from typing import Any, Callable, Mapping

from .alerts import OperationalAlert, evaluate_operational_alerts
from .codex_accounts import read_codex_pool_status
from .diagnostics import run_doctor
from .hermes_health import (
    HermesBotApiHealth,
    HermesGatewayHeartbeat,
    HermesGroupPolicy,
    probe_gateway_heartbeat,
    probe_hermes_bot_api,
    probe_hermes_group_policy,
    restart_hermes_gateway,
    sync_hermes_group_policy,
)
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


def _hermes_health(
    config: HubConfig,
) -> tuple[HermesGroupPolicy, HermesGatewayHeartbeat, HermesBotApiHealth] | None:
    if not config.recovery_plane.enabled:
        return None
    config_path = config.recovery_plane.hermes_config_path
    if config_path is None:
        return None
    expected_chats = tuple(
        item.telegram_chat_id for item in config.projects if item.telegram_chat_id is not None
    )
    return (
        probe_hermes_group_policy(expected_chats),
        probe_gateway_heartbeat(config_path.parent / "state" / "gateway.heartbeat"),
        probe_hermes_bot_api(config_path.parent / ".env"),
    )


def run_monitor_once(
    config: HubConfig,
    *,
    notify: bool,
    repair: bool = False,
    cooldown_seconds: int = 60 * 60,
) -> dict[str, object]:
    state = HubState.open(config.state_path)
    try:
        snapshot = state.status_snapshot()
        hermes_health = _hermes_health(config)
        repairs: list[str] = []
        if repair and hermes_health is not None:
            policy, heartbeat, api = hermes_health
            pending_stuck = False
            if api.ok and api.pending_updates is not None and api.pending_updates > 0:
                time.sleep(3)
                config_path = config.recovery_plane.hermes_config_path
                assert config_path is not None
                confirmation = probe_hermes_bot_api(config_path.parent / ".env")
                pending_stuck = bool(
                    confirmation.ok
                    and confirmation.pending_updates is not None
                    and confirmation.pending_updates > 0
                )
                api = confirmation
                hermes_health = (policy, heartbeat, api)
            needs_repair = not policy.ok or not heartbeat.ok or pending_stuck
            if needs_repair and state.claim_alert_delivery(
                "repair:hermes-gateway", cooldown_seconds=cooldown_seconds
            ):
                try:
                    expected_chats = tuple(
                        item.telegram_chat_id
                        for item in config.projects
                        if item.telegram_chat_id is not None
                    )
                    if not policy.ok and sync_hermes_group_policy(expected_chats):
                        repairs.append("hermes_group_policy_synced")
                    restart_hermes_gateway(config.recovery_plane.hermes_service)
                    repairs.append("hermes_gateway_restarted")
                except Exception:
                    repairs.append("hermes_repair_failed")
                else:
                    config_path = config.recovery_plane.hermes_config_path
                    assert config_path is not None
                    policy = probe_hermes_group_policy(expected_chats)
                    api = probe_hermes_bot_api(config_path.parent / ".env")
                    hermes_health = (policy, heartbeat, api)
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
        hermes_telegram: dict[str, object] | None = None
        if hermes_health is not None:
            policy, heartbeat, api = hermes_health
            hermes_telegram = {
                "policy_ok": policy.ok,
                "heartbeat_ok": heartbeat.ok,
                "api_ok": api.ok,
                "pending_updates": api.pending_updates,
            }
            recovery_status["hermes"] = bool(
                recovery_status.get("hermes", False) and policy.ok and heartbeat.ok and api.ok
            )
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot=snapshot,
            doctor_ok=bool(doctor["ok"]),
            recovery_status=recovery_status or None,
            telegram_access=_telegram_access(config),
            hermes_telegram=hermes_telegram,
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
            if config.recovery_plane.enabled and recovery_status.get("hermes", False):
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
            "repairs": repairs,
        }
    finally:
        state.close()
