from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .codex_accounts import CodexPoolStatus

DEFAULT_LOW_QUOTA_PERCENT = 5


@dataclass(frozen=True, slots=True)
class OperationalAlert:
    key: str
    code: str
    severity: str
    message: str


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def evaluate_operational_alerts(
    *,
    pool: CodexPoolStatus,
    state_snapshot: Mapping[str, object],
    doctor_ok: bool,
    recovery_status: Mapping[str, bool] | None = None,
    telegram_access: Mapping[tuple[str, str], bool] | None = None,
    hermes_telegram: Mapping[str, object] | None = None,
    runtime_health: Mapping[str, object] | None = None,
    now: datetime | None = None,
    low_quota_percent: int = DEFAULT_LOW_QUOTA_PERCENT,
    stuck_after_seconds: int = 15 * 60,
) -> tuple[OperationalAlert, ...]:
    evaluated_at = now or datetime.now(timezone.utc)
    alerts: list[OperationalAlert] = []
    if runtime_health is not None:
        health_items: list[Mapping[str, object]] = []
        for name in ("controller", "sender"):
            value = runtime_health.get(name)
            if isinstance(value, Mapping):
                health_items.append(value)
        workers = runtime_health.get("provider_workers")
        if isinstance(workers, list):
            health_items.extend(item for item in workers if isinstance(item, Mapping))
        for item in health_items:
            status = str(item.get("status") or "unknown")
            if status in {"healthy", "not_configured"}:
                continue
            component = str(item.get("component") or "runtime")[:32]
            instance_id = str(item.get("instance_id") or "unknown")[:128]
            agent_id = str(item.get("agent_id") or "")[:64]
            label = f"provider worker {agent_id}" if component == "provider_worker" else component
            alerts.append(
                OperationalAlert(
                    f"runtime:{component}:{instance_id}",
                    f"{component}_health_{status}",
                    "warning" if status == "degraded" else "error",
                    f"The configured {label} runtime health is {status}; "
                    "inspect the local cached status and service logs.",
                )
            )
    if not doctor_ok:
        alerts.append(
            OperationalAlert(
                "deployment:doctor",
                "deployment_unhealthy",
                "error",
                "Project Hub diagnostics are unhealthy; run the local doctor report.",
            )
        )
    if recovery_status is not None:
        hermes_ok = recovery_status.get("hermes", False)
        tlive_ok = recovery_status.get("tlive", False)
        if not hermes_ok:
            alerts.append(
                OperationalAlert(
                    "recovery:hermes",
                    "hermes_recovery_unavailable",
                    "warning",
                    "The independent Hermes Telegram recovery channel is unavailable.",
                )
            )
        if not tlive_ok:
            alerts.append(
                OperationalAlert(
                    "recovery:tlive",
                    "tlive_recovery_unavailable",
                    "warning",
                    "The tlive monitoring and remote-approval channel is unavailable.",
                )
            )
        if not hermes_ok and not tlive_ok:
            alerts.append(
                OperationalAlert(
                    "recovery:all",
                    "recovery_plane_unavailable",
                    "error",
                    "Both independent recovery channels are unavailable; local intervention is required.",
                )
            )
    if telegram_access is not None:
        for (agent_id, project_id), accessible in telegram_access.items():
            if accessible:
                continue
            alerts.append(
                OperationalAlert(
                    f"telegram:{agent_id}:{project_id}",
                    "telegram_bot_group_unavailable",
                    "error" if agent_id == "codex" else "warning",
                    f"The {agent_id} bot cannot access the {project_id} project group.",
                )
            )
    if hermes_telegram is not None:
        if hermes_telegram.get("policy_ok") is False:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:policy",
                    "hermes_group_policy_incomplete",
                    "error",
                    "Hermes does not allow every registered Telegram project group.",
                )
            )
        if hermes_telegram.get("heartbeat_ok") is False:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:heartbeat",
                    "hermes_gateway_heartbeat_stale",
                    "error",
                    "Hermes Gateway is active but its event-loop heartbeat is stale.",
                )
            )
        if hermes_telegram.get("api_ok") is False:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:api",
                    "hermes_telegram_api_unavailable",
                    "warning",
                    "Hermes Telegram Bot API liveness probe failed.",
                )
            )
        pending = hermes_telegram.get("pending_updates")
        if isinstance(pending, int) and pending > 0:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:pending",
                    "hermes_telegram_updates_pending",
                    "warning",
                    f"Hermes has {pending} Telegram update(s) waiting for its gateway.",
                )
            )
    if not pool.available:
        alerts.append(
            OperationalAlert(
                "codex:pool",
                "codex_pool_unavailable",
                "error",
                "Codex account-pool status is unavailable.",
            )
        )
    else:
        if not pool.rotation_enabled:
            alerts.append(
                OperationalAlert(
                    "codex:rotation",
                    "codex_rotation_disabled",
                    "error",
                    "Codex account rotation is disabled.",
                )
            )
        usable_replacement = any(account.availability == "ready" for account in pool.accounts)
        for account in pool.accounts:
            identity = f" ({account.identity_hint})" if account.identity_hint else ""
            fresh_quota_exhausted = not account.quota_stale and any(
                remaining is not None and remaining <= low_quota_percent
                for remaining in (account.five_hour_remaining, account.weekly_remaining)
            )
            if account.availability == "unavailable" and not fresh_quota_exhausted:
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:unavailable",
                        "codex_account_unavailable",
                        "error",
                        f"Codex account {account.index}{identity} is unavailable; authentication needs attention.",
                    )
                )
            quota_alert_relevant = account.active or not usable_replacement
            if (
                quota_alert_relevant
                and not account.quota_stale
                and account.five_hour_remaining is not None
                and account.five_hour_remaining <= low_quota_percent
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:5h-low",
                        "codex_5h_low",
                        "warning",
                        f"Codex account {account.index}{identity} has {account.five_hour_remaining}% of its 5-hour quota left.",
                    )
                )
            if (
                quota_alert_relevant
                and not account.quota_stale
                and account.weekly_remaining is not None
                and account.weekly_remaining <= low_quota_percent
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:week-low",
                        "codex_weekly_low",
                        "warning",
                        f"Codex account {account.index}{identity} has {account.weekly_remaining}% of its weekly quota left.",
                    )
                )
    pending = state_snapshot.get("pending_dispatches")
    if isinstance(pending, list):
        for item in pending:
            if not isinstance(item, dict):
                continue
            updated_at = _timestamp(item.get("updated_at"))
            if (
                updated_at is None
                or (evaluated_at - updated_at).total_seconds() <= stuck_after_seconds
            ):
                continue
            topic_id = item.get("topic_id")
            agent_id = str(item.get("agent_id") or "unknown")[:32]
            alerts.append(
                OperationalAlert(
                    f"dispatch:topic:{topic_id}:agent:{agent_id}",
                    "dispatch_stuck",
                    "error",
                    f"A {agent_id} dispatch in topic {topic_id} has been running for over 15 minutes.",
                )
            )
    return tuple(alerts)
