from __future__ import annotations

import html
import subprocess
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from .alerts import OperationalAlert, evaluate_operational_alerts
from .catalog_refresh import refresh_provider_catalogs
from .diagnostic_log import survived
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
from .hub_config import HubConfig, OperationalAlertSettings, read_telegram_token
from .incident_journal import IncidentJournal, IncidentJournalBusy, IncidentJournalError
from .monitor_health import MonitorHealth
from .provider_catalog_cache import ProviderCatalogCache
from .runtime_health import project_runtime_health
from .state import HubState
from .telegram import TelegramBotApi, TelegramError


def _render(alerts: tuple[OperationalAlert, ...]) -> str:
    lines = ["Project Hub operational alert"]
    lines.extend(f"[{alert.severity.upper()}] {alert.message}" for alert in alerts)
    return html.escape("\n".join(lines))


def _destination(settings: OperationalAlertSettings) -> tuple[int, int] | None:
    """Return the one explicitly configured Hub operations topic, or fail closed."""
    if settings.telegram_chat_id is None or settings.telegram_thread_id is None:
        return None
    return settings.telegram_chat_id, settings.telegram_thread_id


def _operational_telegram(config: HubConfig) -> tuple[TelegramBotApi, str]:
    """Open the controller identity used for Hub-owned operational messages."""
    if config.hub_bot is None:
        raise RuntimeError("Hub bot is required for operational notifications")
    identity = "hub"
    token_file = config.hub_bot.token_file
    return TelegramBotApi(read_telegram_token(token_file, identity)), identity


def _claim_operational_alert(
    state: HubState, alert: OperationalAlert, *, cooldown_seconds: int
) -> bool:
    del cooldown_seconds
    return state.claim_alert_transition(f"{alert.key}:operations")


def _release_resolved_operational_alerts(
    state: HubState, alerts: tuple[OperationalAlert, ...], *, unobserved_keys: tuple[str, ...] = ()
) -> None:
    state.reconcile_alert_transitions(
        active_keys=tuple(
            f"{key}:operations" for key in (*[alert.key for alert in alerts], *unobserved_keys)
        ),
        suffix=":operations",
    )


def _send_hermes(
    target: str,
    message: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    completed = run(
        ("hermes", "send", "--to", target, "--quiet", "--file", "-"),
        input=message,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Hermes recovery-channel delivery failed")


def _telegram_access(
    config: HubConfig, *, errors: list[TelegramError] | None = None
) -> dict[tuple[str, str], bool | None]:
    """True/False require a response; transport and invalid responses are unknown."""
    access: dict[tuple[str, str], bool | None] = {}
    for agent in config.agents:
        if agent.managed_externally or agent.token_file is None:
            continue
        telegram = TelegramBotApi(agent.token_file.read_text(encoding="utf-8").strip())
        for project in config.projects:
            if project.telegram_chat_id is None:
                continue
            try:
                result = telegram.call("getChat", chat_id=project.telegram_chat_id)
            except TelegramError as error:
                rejected = error.failure_class in {
                    "api_http",
                    "api_rejection",
                } and error.status_code in {400, 403}
                access[(agent.agent_id, project.project_id)] = False if rejected else None
                if not rejected and errors is not None:
                    errors.append(error)
            else:
                valid = (
                    isinstance(result, dict)
                    and type(result.get("id")) is int
                    and result["id"] == project.telegram_chat_id
                )
                access[(agent.agent_id, project.project_id)] = True if valid else None
                if not valid and errors is not None:
                    errors.append(
                        TelegramError(
                            "Invalid getChat result",
                            operation="api_call",
                            failure_class="invalid_response",
                        )
                    )
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
    state = HubState.open_existing(config.state_path)
    journal = IncidentJournal(config.state_path.resolve().with_name("incident-journal.json"))
    try:
        with ExitStack() as cycle:
            available: IncidentJournal | None = journal
            try:
                cycle.enter_context(journal.cycle())
            except IncidentJournalBusy:
                return {"ok": False, "error_code": "monitor_busy"}
            except IncidentJournalError as error:
                survived("monitoring.incident_lock", error)
                available = None
            return _run_monitor_once(
                config,
                state,
                available,
                notify=notify,
                repair=repair,
                cooldown_seconds=cooldown_seconds,
            )
    finally:
        state.close()


def _project_incidents(
    journal: IncidentJournal | None,
    alerts: tuple[OperationalAlert, ...],
    *,
    unobserved_keys: tuple[str, ...] = (),
) -> dict[str, object]:
    try:
        if journal is None:
            raise IncidentJournalError()
        snapshot = journal.project(
            alerts, now=datetime.now(timezone.utc), unobserved_keys=unobserved_keys
        )
    except IncidentJournalError as error:
        survived("monitoring.incident_projection", error)
        return {"status": "unavailable", "error_code": "incident_journal_unavailable"}
    return {
        "status": "updated",
        "open_episodes": sum(row["resolved_at"] is None for row in snapshot["episodes"]),
        "resolved_episodes": sum(row["resolved_at"] is not None for row in snapshot["episodes"]),
        "omitted_open_count": snapshot["omitted_open_count"],
        "omitted_resolved_count": snapshot["omitted_resolved_count"],
    }


def _run_monitor_once(
    config: HubConfig,
    state: HubState,
    journal: IncidentJournal | None,
    *,
    notify: bool,
    repair: bool,
    cooldown_seconds: int,
) -> dict[str, object]:
    monitor_started = datetime.now(timezone.utc)
    monitor_marker = uuid.uuid4().hex
    monitor_completed = False
    health = MonitorHealth(state, started=monitor_started, marker=monitor_marker)
    health.publish()
    try:
        snapshot = state.status_snapshot()
        catalog_refresh = refresh_provider_catalogs(config)
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
        probe_errors: list[TelegramError] = []
        access = _telegram_access(config, errors=probe_errors)
        unobserved_keys = tuple(
            f"telegram:{agent}:{project}"
            for (agent, project), value in access.items()
            if value is None
        )
        health.observe(probe_errors, checked=bool(access))
        health.publish()
        alerts = evaluate_operational_alerts(
            state_snapshot=snapshot,
            doctor_ok=bool(doctor["ok"]),
            recovery_status=recovery_status or None,
            telegram_access=access,
            hermes_telegram=hermes_telegram,
            runtime_health=project_runtime_health(state, config),
            codex_config_proxy_ok=next(
                (
                    bool(check.get("ok"))
                    for check in doctor_checks
                    if isinstance(check, dict) and check.get("name") == "codex_config_proxy"
                ),
                None,
            ),
        )
        stale_catalogs = ProviderCatalogCache(
            config.state_path.with_name("provider-model-catalogs.json")
        ).stale_agents()
        alerts += tuple(
            OperationalAlert(
                f"catalog:{agent_id}:stale",
                "provider_catalog_stale",
                "warning",
                "A configured provider model catalog is stale after a failed refresh; "
                "the last known-good local catalog remains active.",
            )
            for agent_id in stale_catalogs
        )
        incident_status = _project_incidents(journal, alerts, unobserved_keys=unobserved_keys)
        delivered: list[str] = []
        if notify:
            _release_resolved_operational_alerts(state, alerts, unobserved_keys=unobserved_keys)
        if notify and alerts:
            destination = _destination(config.operational_alerts)
            operations_due = tuple(
                alert
                for alert in alerts
                if destination is not None
                and _claim_operational_alert(state, alert, cooldown_seconds=cooldown_seconds)
            )
            if operations_due and destination is not None:
                chat_id, thread_id = destination
                rendered = _render(operations_due)
                try:
                    telegram, sender_identity = _operational_telegram(config)
                    telegram.send_html(chat_id, thread_id, rendered[:4090])
                except Exception:
                    try:
                        if not (
                            config.recovery_plane.enabled and recovery_status.get("hermes", False)
                        ):
                            raise RuntimeError("Hermes recovery channel is unavailable")
                        _send_hermes(f"telegram:{chat_id}:{thread_id}", html.unescape(rendered))
                    except Exception:
                        for alert in operations_due:
                            state.release_alert_delivery(f"{alert.key}:operations")
                        raise
                    else:
                        delivered.extend(
                            f"{alert.code}:hermes-fallback" for alert in operations_due
                        )
                else:
                    delivered.extend(f"{alert.code}:{sender_identity}" for alert in operations_due)
        result = {
            "ok": incident_status["status"] == "updated"
            and not any(alert.severity == "error" for alert in alerts),
            "alerts": [asdict(alert) for alert in alerts],
            "delivered": delivered,
            "repairs": repairs,
            "catalog_refresh": asdict(catalog_refresh),
            "reliability": snapshot["reliability"],
            "incident_journal": incident_status,
            "telegram_access": {
                name: sum(value is status for value in access.values())
                for name, status in (("available", True), ("unavailable", False), ("unknown", None))
            },
        }
        completed_at = datetime.now(timezone.utc)
        health.publish(
            success_at=completed_at if incident_status["status"] == "updated" else None,
            error_code=None
            if incident_status["status"] == "updated"
            else "incident_journal_unavailable",
        )
        monitor_completed = True
        return result
    finally:
        if not monitor_completed:
            try:
                health.publish(error_code="monitor_cycle_error")
            except Exception as survived_error:
                survived("monitoring.error_health_publish", survived_error)
