from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .codex_accounts import CodexPoolStatus


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
    now: datetime | None = None,
    low_quota_percent: int = 10,
    stuck_after_seconds: int = 15 * 60,
) -> tuple[OperationalAlert, ...]:
    evaluated_at = now or datetime.now(timezone.utc)
    alerts: list[OperationalAlert] = []
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
        for account in pool.accounts:
            if account.availability == "unavailable":
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:unavailable",
                        "codex_account_unavailable",
                        "error",
                        f"Codex account {account.index} is unavailable; authentication needs attention.",
                    )
                )
            if (
                account.five_hour_remaining is not None
                and account.five_hour_remaining <= low_quota_percent
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:5h-low",
                        "codex_5h_low",
                        "warning",
                        f"Codex account {account.index} has {account.five_hour_remaining}% of its 5-hour quota left.",
                    )
                )
            if (
                account.weekly_remaining is not None
                and account.weekly_remaining <= low_quota_percent
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:week-low",
                        "codex_weekly_low",
                        "warning",
                        f"Codex account {account.index} has {account.weekly_remaining}% of its weekly quota left.",
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
