from __future__ import annotations

from collections.abc import Mapping

from .operational_alert import OperationalAlert

MAX_DELIVERY_AGE_SECONDS = 5 * 60


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def evaluate_reliability_alerts(
    telemetry: Mapping[str, object],
) -> tuple[OperationalAlert, ...]:
    """Evaluate passive stalled-work/outbox signals without provider access."""
    alerts: list[OperationalAlert] = []
    stalled = _positive_int(telemetry.get("stalled_provider_work"))
    if stalled is not None:
        alerts.append(
            OperationalAlert(
                "reliability:provider-work-stalled",
                "provider_work_stalled",
                "error",
                f"Hub has {stalled} stalled provider job(s): an expired lease or ready work "
                "waiting over 15 minutes. Inspect worker health and leases without replaying tasks.",
            )
        )
    pending = _positive_int(telemetry.get("pending_delivery"))
    delivery_age = _positive_int(telemetry.get("oldest_delivery_age_seconds"))
    if pending is not None and delivery_age is not None and delivery_age > MAX_DELIVERY_AGE_SECONDS:
        alerts.append(
            OperationalAlert(
                "reliability:telegram-delivery-age",
                "telegram_delivery_age_exceeded",
                "error",
                "A committed Telegram delivery has waited for over 5 minutes; inspect the "
                "sender and persisted retry deadline without repeating provider work.",
            )
        )
    pending_progress = _positive_int(telemetry.get("pending_progress_delivery"))
    progress_age = _positive_int(telemetry.get("oldest_progress_delivery_age_seconds"))
    if (
        pending_progress is not None
        and progress_age is not None
        and progress_age > MAX_DELIVERY_AGE_SECONDS
    ):
        alerts.append(
            OperationalAlert(
                "reliability:progress-delivery-age",
                "progress_delivery_age_exceeded",
                "warning",
                "A provider progress update has waited for over 5 minutes; inspect the "
                "sender and persisted retry deadline. The provider job is unchanged.",
            )
        )
    unresolved = _positive_int(telemetry.get("unresolved_uncertain_execution"))
    if unresolved is not None:
        alerts.append(
            OperationalAlert(
                "reliability:unresolved-provider-outcome",
                "unresolved_provider_outcome",
                "warning",
                f"Hub has {unresolved} unresolved provider outcome(s); run the local "
                "indeterminate audit before deciding any next action.",
            )
        )
    return tuple(alerts)
