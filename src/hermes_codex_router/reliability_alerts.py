from __future__ import annotations

from collections.abc import Mapping

from .operational_alert import OperationalAlert

MAX_QUEUE_AGE_SECONDS = 15 * 60
MAX_DELIVERY_AGE_SECONDS = 5 * 60


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def evaluate_reliability_alerts(
    telemetry: Mapping[str, object],
) -> tuple[OperationalAlert, ...]:
    """Evaluate passive queue/outbox thresholds without provider or network access."""
    alerts: list[OperationalAlert] = []
    queued = _positive_int(telemetry.get("queued_work"))
    queue_age = _positive_int(telemetry.get("oldest_queue_age_seconds"))
    if queued is not None and queue_age is not None and queue_age > MAX_QUEUE_AGE_SECONDS:
        alerts.append(
            OperationalAlert(
                "reliability:provider-queue-age",
                "provider_queue_age_exceeded",
                "error",
                "Provider work has remained nonterminal for over 15 minutes; inspect the "
                "owning worker and its lease without replaying the task.",
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
